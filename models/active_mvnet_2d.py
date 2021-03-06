import os
import sys
import numpy as np
import tensorflow as tf
import tensorflow.contrib.slim as slim
from utils import util
from utils import tf_util

from env_data.shapenet_env import ShapeNetEnv, trajectData  
from lsm.ops import convgru, convlstm, collapse_dims, uncollapse_dims 
from util_unproj import Unproject_tools 
import other
from tensorflow import summary as summ

def lrelu(x, leak=0.2, name='lrelu'):
    with tf.variable_scope(name):
        f1 = 0.5 * (1+leak)
        f2 = 0.5 * (1-leak)
        return f1*x + f2 * abs(x)
    
class ActiveMVnet2D(object):
    def __init__(self, FLAGS):
        self.FLAGS = FLAGS
        #self.senv = ShapeNetEnv(FLAGS)
        #self.replay_mem = ReplayMemory(FLAGS)
        self.unproj_net = Unproject_tools(FLAGS)

        self.activation_fn = lrelu
        self.counter = tf.Variable(0, trainable=False, dtype=tf.int32)

        self._create_placeholders()
        self._create_ground_truth_voxels()
        self._create_network()
        self._create_loss()
        #if FLAGS.is_training:
        self._create_optimizer()
        self._create_summary()
        self._create_collections()
        
        # Add ops to save and restore all variable 
        self.saver = tf.train.Saver()
        self.pretrain_saver = tf.train.Saver(max_to_keep=None)
        
        # create a sess
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        config.log_device_placement = False
        self.sess = tf.Session(config=config)
        self.sess.run(tf.global_variables_initializer())

        self.train_writer = tf.summary.FileWriter(os.path.join(FLAGS.LOG_DIR, 'train'), self.sess.graph)

    def _create_placeholders(self):
        
        self.is_training = tf.placeholder(tf.bool, shape=(), name='is_training')

        self.train_provider = ShapeProvider(self.FLAGS)
        self.test_provider = ShapeProvider(self.FLAGS, batch_size = 1)

        self.train_provider.make_tf_ph()
        self.test_provider.make_tf_ph()
        
        self.RGB_list_batch = self.train_provider.rgb_ph
        self.invZ_list_batch = self.train_provider.invz_ph
        self.mask_list_batch = self.train_provider.mask_ph
        self.azimuth_list_batch = self.train_provider.azimuth_ph
        self.elevation_list_batch = self.train_provider.elevation_ph
        self.action_list_batch = self.train_provider.action_ph
        self.vox_batch = self.train_provider.vox_ph
        
        self.RGB_list_test = self.test_provider.rgb_ph
        self.invZ_list_test = self.test_provider.invz_ph
        self.mask_list_test = self.test_provider.mask_ph
        self.azimuth_list_test = self.test_provider.azimuth_ph
        self.elevation_list_test = self.test_provider.elevation_ph
        self.action_list_test = self.test_provider.action_ph
        self.vox_test = self.test_provider.vox_ph

    def _create_ground_truth_voxels(self):
        az0_train = self.azimuth_list_batch[:,0,0]
        el0_train = self.elevation_list_batch[:,0,0]
        az0_test = self.azimuth_list_test[:,0,0]
        el0_test = self.elevation_list_test[:,0,0]

        def rotate_voxels(vox, az0, el0):
            vox = tf.expand_dims(vox, axis = 4)
            #negative sign is important -- although i'm not sure how it works
            R = other.voxel.get_transform_matrix_tf(-az0, el0)
            return other.voxel.rotate_voxel(other.voxel.transformer_preprocess(vox), R)

        def tile_voxels(x):
            return tf.tile(
                tf.expand_dims(x, axis = 1),
                [1, self.FLAGS.max_episode_length, 1 ,1 ,1, 1]
            )

        self.rotated_vox_batch = rotate_voxels(self.vox_batch, az0_train, el0_train)
        self.rotated_vox_test = rotate_voxels(self.vox_test, az0_test, el0_test)

        self.vox_list_batch = tile_voxels(tf.expand_dims(self.vox_batch, axis=-1))
        self.vox_list_test = tile_voxels(tf.expand_dims(self.vox_test, axis=-1))
        
        self.rotated_vox_list_batch = tile_voxels(self.rotated_vox_batch)
        self.rotated_vox_list_test = tile_voxels(self.rotated_vox_test)

        if self.FLAGS.debug_mode:
            summ.histogram('ground_truth_voxels', self.rotated_vox_batch)
        
    def _create_dqn_two_stream(self, rgb, feat, trainable=True, if_bn=False, reuse=False,
                               scope_name='dqn_two_stream'):
        
        with tf.variable_scope(scope_name) as scope:
            if reuse:
                scope.reuse_variables()
            
            if if_bn:
                batch_normalizer_gen = slim.batch_norm
                #batch_norm_params_gen = {'is_training': self.is_training, 'decay': self.FLAGS.bn_decay}
                batch_norm_params_gen = {'is_training': self.is_training, 
                                         'decay': self.FLAGS.bn_decay,
                                         'epsilon': 1e-5,
                                         'scale': True,
                                         'updates_collections': None}
                #batch_normalizer_gen = None
                #batch_norm_params_gen = None
            else:
                #self._print_arch('=== NOT Using BN for GENERATOR!')
                batch_normalizer_gen = None
                batch_norm_params_gen = None

            if self.FLAGS.if_l2Reg:
                weights_regularizer = slim.l2_regularizer(1e-5)
            else:
                weights_regularizer = None
            
            with slim.arg_scope([slim.fully_connected, slim.conv2d, slim.conv3d],
                    activation_fn=self.activation_fn,
                    trainable=trainable,
                    normalizer_fn=batch_normalizer_gen,
                    normalizer_params=batch_norm_params_gen,
                    weights_regularizer=weights_regularizer):
                
                net_rgb = slim.conv2d(rgb, 16, kernel_size=[3,3], stride=[2,2], padding='SAME', scope='rgb_conv1')
                net_rgb = slim.conv2d(net_rgb, 32, kernel_size=[3,3], stride=[2,2], padding='SAME', scope='rgb_conv2')
                net_rgb = slim.conv2d(net_rgb, 64, kernel_size=[3,3], stride=[2,2], padding='SAME', scope='rgb_conv3')
                net_rgb = slim.conv2d(net_rgb, 64, kernel_size=[3,3], stride=[2,2], padding='SAME', scope='rgb_conv4')
                net_rgb = slim.conv2d(net_rgb, 128, kernel_size=[3,3], stride=[2,2], padding='SAME', scope='rgb_conv5')
                net_rgb = slim.flatten(net_rgb, scope='rgb_flatten')

                net_feat = slim.fully_connected(feat, 2048, scope='feat_fc1')
                
                net_feat = tf.concat([net_rgb, net_feat], axis=1)
                net_feat = slim.fully_connected(net_feat, 2048, scope='fc6')
                net_feat = slim.fully_connected(net_feat, 4096, scope='fc7')
                logits = slim.fully_connected(net_feat, self.FLAGS.action_num, activation_fn=None, normalizer_fn=None, scope='fc8')

                return tf.nn.softmax(logits), logits
    
    def _create_encoder_2d(self, rgb, trainable=True, if_bn=False, reuse=False, scope_name='unet_encoder'):

        with tf.variable_scope(scope_name) as scope:
            if reuse:
                scope.reuse_variables()

            if if_bn:
                batch_normalizer_gen = slim.batch_norm
                batch_norm_params_gen = {'is_training': self.is_training, 
                                         'decay': self.FLAGS.bn_decay,
                                         'epsilon': 1e-5,
                                         'scale': True,
                                         'updates_collections': None}
            else:
                #self._print_arch('=== NOT Using BN for GENERATOR!')
                batch_normalizer_gen = None
                batch_norm_params_gen = None

            if self.FLAGS.if_l2Reg:
                weights_regularizer = slim.l2_regularizer(1e-5)
            else:
                weights_regularizer = None


            with slim.arg_scope([slim.fully_connected, slim.conv2d],
                    activation_fn=self.activation_fn,
                    trainable=trainable,
                    normalizer_fn=batch_normalizer_gen,
                    normalizer_params=batch_norm_params_gen,
                    weights_regularizer=weights_regularizer):

                net_down1 = slim.conv2d(rgb, 64, kernel_size=[3,3], stride=[2,2], padding='SAME', scope='ae_conv1')
                net_down2 = slim.conv2d(net_down1, 128, kernel_size=[3,3], stride=[2,2], padding='SAME', scope='ae_conv2')
                net_down3 = slim.conv2d(net_down2, 256, kernel_size=[3,3], stride=[2,2], padding='SAME', scope='ae_conv3')
                net_down4 = slim.conv2d(net_down3, 256, kernel_size=[3,3], stride=[2,2], padding='SAME', scope='ae_conv4')
                net_bottleneck = slim.conv2d(net_down4, 256, kernel_size=[4,4], stride=[2,2], padding='SAME', scope='ae_conv5')

        return net_bottleneck
    
    def _create_decoder_3d(self, z_rgb, out_channel=1, trainable=True, if_bn=False, reuse=False, scope_name='unet_decoder'):

        with tf.variable_scope(scope_name) as scope:
            if reuse:
                scope.reuse_variables()

            if if_bn:
                batch_normalizer_gen = slim.batch_norm
                #batch_norm_params_gen = {'is_training': self.is_training, 'decay': self.FLAGS.bn_decay}
                batch_norm_params_gen = {'is_training': self.is_training, 
                                         'decay': self.FLAGS.bn_decay,
                                         'epsilon': 1e-5,
                                         'scale': True,
                                         'updates_collections': None}
            else:
                #self._print_arch('=== NOT Using BN for GENERATOR!')
                batch_normalizer_gen = None
                batch_norm_params_gen = None

            if self.FLAGS.if_l2Reg:
                weights_regularizer = slim.l2_regularizer(1e-5)
            else:
                weights_regularizer = None


            with slim.arg_scope([slim.fully_connected, slim.conv3d_transpose],
                    activation_fn=self.activation_fn,
                    trainable=trainable,
                    normalizer_fn=batch_normalizer_gen,
                    normalizer_params=batch_norm_params_gen,
                    weights_regularizer=weights_regularizer):

                net_up5 = slim.conv3d_transpose(z_rgb, 128, kernel_size=4, stride=2, padding='SAME', \
                    scope='unet_deconv6')
                net_up4 = slim.conv3d_transpose(net_up5, 128, kernel_size=3, stride=1, padding='SAME', \
                    scope='unet_deconv5')
                net_up4_1 = slim.conv3d_transpose(net_up5, 128, kernel_size=1, stride=1, padding='SAME', \
                    scope='unet_deconv5_1')
                net_up3 = slim.conv3d_transpose(net_up4+net_up4_1, 64, kernel_size=4, stride=2, padding='SAME', \
                    scope='unet_deconv4')
                net_up2 = slim.conv3d_transpose(net_up3, 64, kernel_size=3, stride=1, padding='SAME', \
                    scope='unet_deconv3')
                net_up2_1 = slim.conv3d_transpose(net_up3, 64, kernel_size=1, stride=1, padding='SAME', \
                    scope='unet_deconv3_1')
                net_up1 = slim.conv3d_transpose(net_up2+net_up2_1, 32, kernel_size=4, stride=2, padding='SAME', \
                    scope='unet_deconv2')
                net_up0 = slim.conv3d_transpose(net_up1, 32, kernel_size=3, stride=1, padding='SAME', \
                    scope='unet_deconv1')
                net_up0_1 = slim.conv3d_transpose(net_up1, 32, kernel_size=1, stride=1, padding='SAME', \
                    scope='unet_deconv1_1')
                net_out_ = slim.conv3d_transpose(net_up0+net_up0_1, 32, kernel_size=4, stride=2, padding='SAME', \
                    scope='unet_out_')
                net_out = slim.conv3d_transpose(net_out_, out_channel, kernel_size=1, stride=1, padding='SAME', \
                    activation_fn=None, normalizer_fn=None, normalizer_params=None, scope='unet_out')

        return tf.nn.sigmoid(net_out), net_out
    
    def _create_unet3d(self, vox_feat, channels, trainable=True, if_bn=False, reuse=False, scope_name='unet_3d'):

        if self.FLAGS.unet_name == 'U_SAME':
            return other.nets.unet_same(
                vox_feat, channels, self.FLAGS, trainable = trainable, if_bn = if_bn, reuse = reuse,
                is_training = self.is_training, activation_fn = self.activation_fn, scope_name = scope_name
            )
        elif self.FLAGS.unet_name == 'U_VALID':
            #can't run summaries for test
            #debug = int(vox_feat.get_shape().as_list()[0]) > self.FLAGS.max_episode_length
            debug = False
            with tf.variable_scope(scope_name, reuse = reuse):
                return other.nets.voxel_net_3d_v2(vox_feat, bn = if_bn, return_logits = True, debug = debug)
        elif self.FLAGS.unet_name == 'OUTLINE':
            return vox_feat, tf.zeros_like(vox_feat)
        else:
            raise Exception, 'not a valid unet name'
    
    def _create_aggregator64(self, unproj_grids, channels, trainable=True, if_bn=False, reuse=False,
                             scope_name='aggr_64'):

        if self.FLAGS.agg_name == 'GRU':
            return other.nets.gru_aggregator(
                unproj_grids, channels, self.FLAGS, trainable = trainable, if_bn = if_bn, reuse = reuse,
                is_training = self.is_training, activation_fn = self.activation_fn, scope_name = scope_name
            )
        elif self.FLAGS.agg_name == 'POOL':
            return other.nets.pooling_aggregator(
                unproj_grids, channels, self.FLAGS, trainable = trainable, reuse = reuse,
                is_training = self.is_training, scope_name = scope_name
            )
        elif self.FLAGS.unet_name == 'OUTLINE':
            #bs = int(unproj_grids.get_shape()[0]) / self.FLAGS.max_episode_length
            #unproj_grids = uncollapse_dims(unproj_grids, bs, self.FLAGS.max_episode_length)
            rvals = [tf.reduce_max(unproj_grids[:,:i+1,:,:,:,-1:], axis = 1)
                     for i in range(self.FLAGS.max_episode_length)]
            return tf.stack(rvals, axis = 1)
        else:
            raise Exception, 'not a valid agg name'

    def _create_aggregator(self, feat_list, channels=4096, trainable=True, reuse=False, scope_name='aggr'):
        with tf.variable_scope(scope_name) as scope:
            if reuse:
                scope.reuse_variables()
            #gru_cell = tf.nn.rnn_cell.LSTMCell(channels, reuse=reuse)
            #rnn_layers = [tf.nn.rnn_cell.GRUCell(channel, reuse=reuse) for channel in [1024, 2048]]
            #multi_rnn_cell = tf.nn.rnn_cell.MultiRNNCell(rnn_layers)
            rnn_cell = tf.nn.rnn_cell.LSTMCell(2048, reuse=reuse)
            outs, _ = tf.nn.dynamic_rnn(rnn_cell, feat_list, dtype=tf.float32)

            return outs

    def _create_fuser(self, feat, channels=128, trainable=True, if_bn=False, reuse=False, scope_name='fuse'):
        with tf.variable_scope(scope_name) as scope:
            if reuse:
                scope.reuse_variables()

            if if_bn:
                batch_normalizer_gen = slim.batch_norm
                batch_norm_params_gen = {'is_training': self.is_training, 
                                         'decay': self.FLAGS.bn_decay,
                                         'epsilon': 1e-5,
                                         'scale': True,
                                         'updates_collections': None}
            else:
                batch_normalizer_gen = None
                batch_norm_params_gen = None

            if self.FLAGS.if_l2Reg:
                weights_regularizer = slim.l2_regularizer(1e-5)
            else:
                weights_regularizer = None

            ## create fuser
            with slim.arg_scope([slim.fully_connected],
                    activation_fn=tf.nn.relu,
                    trainable=trainable,
                    normalizer_fn=batch_normalizer_gen,
                    normalizer_params=batch_norm_params_gen,
                    weights_regularizer=weights_regularizer):
                net_feat = slim.fully_connected(feat, 2048, scope='fc1')
                #net_feat = slim.dropout(net_feat)
                net_feat = slim.fully_connected(net_feat, 4096, scope='fc2')
                #net_feat = slim.dropout(net_feat)

            return net_feat
    
    def _create_encoder_1d(self, feat, channels=128, trainable=True, if_bn=False, reuse=False, scope_name='encoder_1d'):
        with tf.variable_scope(scope_name) as scope:
            if reuse:
                scope.reuse_variables()

            if if_bn:
                batch_normalizer_gen = slim.batch_norm
                batch_norm_params_gen = {'is_training': self.is_training, 
                                         'decay': self.FLAGS.bn_decay,
                                         'epsilon': 1e-5,
                                         'scale': True,
                                         'updates_collections': None}
            else:
                batch_normalizer_gen = None
                batch_norm_params_gen = None

            if self.FLAGS.if_l2Reg:
                weights_regularizer = slim.l2_regularizer(1e-5)
            else:
                weights_regularizer = None

            ## create fuser
            with slim.arg_scope([slim.fully_connected],
                    activation_fn=tf.nn.relu,
                    trainable=trainable,
                    normalizer_fn=batch_normalizer_gen,
                    normalizer_params=batch_norm_params_gen,
                    weights_regularizer=weights_regularizer):
                net_feat = slim.fully_connected(feat, 64, scope='fc1')
                net_feat = slim.fully_connected(net_feat, 128, scope='fc2')

            return net_feat

    def _create_policy_net(self):
        self.rgb_batch_norm = tf.subtract(self.rgb_batch, 0.5)
        self.action_prob, self.logits = self._create_dqn_two_stream(self.rgb_batch_norm, self.vox_batch,
            if_bn=self.FLAGS.if_bn, scope_name='dqn_two_stream')

    def _create_network(self):
        self.RGB_list_batch_norm = tf.subtract(self.RGB_list_batch, 0.5)
        self.RGB_list_test_norm = tf.subtract(self.RGB_list_test, 0.5)

        ## TODO: unproj depth list and merge them using aggregator
        with tf.device('/gpu:0'):

            ## TODO: create features using RGB batch
            ## --------------- train -------------------
            RGB_list_norm = tf.unstack(self.RGB_list_batch_norm, axis=1) 
            invZ_list_batch = tf.unstack(self.invZ_list_batch, axis=1)
            rot_list_batch = tf.unstack(tf.concat([self.azimuth_list_batch, self.elevation_list_batch], axis=-1),
                axis=1)

            rgb_feat_first = self._create_encoder_2d(RGB_list_norm[0], if_bn=self.FLAGS.if_bn,
                scope_name='encoder_rgb_2d')
            invZ_feat_first = self._create_encoder_2d(invZ_list_batch[0], if_bn=self.FLAGS.if_bn,
                scope_name='encoder_invZ_2d')
            rot_feat_first = self._create_encoder_1d(rot_list_batch[0], if_bn=self.FLAGS.if_bn,
                scope_name='encoder_rot_1d')
            rgb_feat_first = tf.reshape(rgb_feat_first, [self.FLAGS.batch_size, -1])
            invZ_feat_first = tf.reshape(invZ_feat_first, [self.FLAGS.batch_size, -1])
            rot_feat_first = tf.reshape(rot_feat_first, [self.FLAGS.batch_size, -1])

            encoder_rgb_reuse = lambda x: self._create_encoder_2d(x, if_bn=self.FLAGS.if_bn, reuse=True,
                scope_name='encoder_rgb_2d')
            encoder_rgb_reuse_test = lambda x: self._create_encoder_2d(x, trainable=False, if_bn=self.FLAGS.if_bn, reuse=True,
                scope_name='encoder_rgb_2d')
            encoder_invZ_reuse = lambda x: self._create_encoder_2d(x, if_bn=self.FLAGS.if_bn, reuse=True,
                scope_name='encoder_invZ_2d')
            encoder_invZ_reuse_test = lambda x: self._create_encoder_2d(x, trainable=False, if_bn=self.FLAGS.if_bn,
                reuse=True, scope_name='encoder_invZ_2d')
            encoder_rot_reuse = lambda x: self._create_encoder_1d(x, trainable=True, if_bn=self.FLAGS.if_bn, reuse=True,
                scope_name='encoder_rot_1d')
            encoder_rot_reuse_test = lambda x: self._create_encoder_1d(x, trainable=False, if_bn=self.FLAGS.if_bn,
                reuse=True, scope_name='encoder_rot_1d')
            rgb_feat_follow = tf.map_fn(encoder_rgb_reuse, tf.stack(RGB_list_norm[1:]))
            rgb_feat_follow = tf.reshape(rgb_feat_follow, [self.FLAGS.max_episode_length-1, self.FLAGS.batch_size, -1])
            invZ_feat_follow = tf.map_fn(encoder_invZ_reuse, tf.stack(invZ_list_batch[1:]))
            invZ_feat_follow = tf.reshape(invZ_feat_follow, [self.FLAGS.max_episode_length-1, self.FLAGS.batch_size, -1])
            rot_feat_follow = tf.map_fn(encoder_rot_reuse, tf.stack(rot_list_batch[1:]))
            rot_feat_follow = tf.reshape(rot_feat_follow, [self.FLAGS.max_episode_length-1, self.FLAGS.batch_size, -1])
            self.rgb_feat = tf.stack([rgb_feat_first]+tf.unstack(rgb_feat_follow), axis=1) 
            self.invZ_feat = tf.stack([invZ_feat_first]+tf.unstack(invZ_feat_follow), axis=1)
            self.rot_feat = tf.stack([rot_feat_first]+tf.unstack(rot_feat_follow), axis=1)
            self.rgbd_feat = tf.concat([self.rgb_feat, self.invZ_feat, self.rot_feat], axis=-1)
            #self.rgbd_feat = tf.unstack(self.rgbd_feat, axis=1)
            ## --------------- train -------------------
            ## --------------- test  -------------------
            RGB_list_norm_test = tf.unstack(self.RGB_list_test_norm, axis=1)
            invZ_list_test = tf.unstack(self.invZ_list_test, axis=1)
            rot_list_test = tf.unstack(tf.concat([self.azimuth_list_test, self.elevation_list_test], axis=-1), axis=1)

            rgb_feat_test_all = tf.map_fn(encoder_rgb_reuse_test, tf.stack(RGB_list_norm_test))
            self.rgb_feat_test = tf.stack(tf.unstack(rgb_feat_test_all), axis=1)
            self.rgb_feat_test = tf.reshape(self.rgb_feat_test, [1, self.FLAGS.max_episode_length, -1])
            invZ_feat_test_all = tf.map_fn(encoder_invZ_reuse_test, tf.stack(invZ_list_test))
            self.invZ_feat_test = tf.stack(tf.unstack(invZ_feat_test_all), axis=1)
            self.invZ_feat_test = tf.reshape(self.invZ_feat_test, [1, self.FLAGS.max_episode_length, -1])
            rot_feat_test_all = tf.map_fn(encoder_rot_reuse_test, tf.stack(rot_list_test))
            self.rot_feat_test = tf.stack(tf.unstack(rot_feat_test_all), axis=1)
            self.rot_feat_test = tf.reshape(self.rot_feat_test, [1, self.FLAGS.max_episode_length, -1])
            self.rgbd_feat_test = tf.concat([self.rgb_feat_test, self.invZ_feat_test, self.rot_feat_test], axis=-1)
            ## --------------- test  -------------------
                
            ## TODO: aggregate on feat using fuse and aggr
            ## --------------- train -------------------
            #feat_fuse_first = self._create_fuser(rgb_feat_first, trainable=True, if_bn=self.FLAGS.if_bn,
            #    scope_name='fuse_feat') 
            self.aggr_feat_list = self._create_aggregator(self.rgbd_feat, scope_name='aggr') ## [BSxExCH] 
            aggr_feat_list = tf.unstack(self.aggr_feat_list, axis=1)
            feat_fuse_first = self._create_fuser(aggr_feat_list[0], trainable=True, if_bn=self.FLAGS.if_bn,
                scope_name='fuse_feat') 
            fuser_reuse = lambda x: self._create_fuser(x, trainable=True, if_bn=self.FLAGS.if_bn, reuse=True,
                scope_name='fuse_feat')
            fuser_reuse_test = lambda x: self._create_fuser(x, trainable=False, if_bn=self.FLAGS.if_bn, reuse=True,
                scope_name='fuse_feat')
            #feat_fuse_follow = tf.map_fn(fuser_reuse, rgb_feat_follow)
            feat_fuse_follow = tf.map_fn(fuser_reuse, tf.stack(aggr_feat_list[1:]))
            self.fuse_feat_list = tf.stack([feat_fuse_first]+tf.unstack(feat_fuse_follow), axis=1) ## [BSxEx4x4xCH]
            #self.fuse_feat_list = tf.reshape(self.fuse_feat_list, [self.FLAGS.batch_size, self.FLAGS.max_episode_length, -1])
            ## --------------- train -------------------
            ## --------------- test  -------------------
            #self.feat_fuse_test = tf.map_fn(fuser_reuse_test, tf.stack(tf.unstack(self.rgb_feat_test, axis=1)))
            self.aggr_feat_list_test = self._create_aggregator(self.rgbd_feat_test, trainable=False, reuse=True,
                scope_name='aggr')
            self.fuse_feat_test = tf.map_fn(fuser_reuse_test, tf.stack(tf.unstack(self.aggr_feat_list_test, axis=1)))
            self.fuse_feat_test = tf.stack(tf.unstack(self.fuse_feat_test), axis=1)
            #self.feat_fuse_test = tf.reshape(self.feat_fuse_test, [1, self.FLAGS.max_episode_length, -1])
            ## --------------- test  -------------------

            ## TODO: fead feat into 3D decoder
            ## --------------- train -------------------
            reshape_size = [self.FLAGS.batch_size, self.FLAGS.max_episode_length,
                4, 4, 4, self.fuse_feat_list.get_shape().as_list()[2]/64]
            aggr_feat_list_ = tf.unstack(tf.reshape(self.fuse_feat_list, reshape_size), axis=1)
            vox_pred_first, vox_logits_first = self._create_decoder_3d(aggr_feat_list_[0], trainable=True, if_bn=self.FLAGS.if_bn,
                scope_name='decoder_3d')
            decoder_reuse = lambda x: self._create_decoder_3d(x, trainable=True, if_bn=self.FLAGS.if_bn,
                reuse=True, scope_name='decoder_3d')
            decoder_reuse_test = lambda x: self._create_decoder_3d(x, trainable=False, if_bn=self.FLAGS.if_bn,
                reuse=True, scope_name='decoder_3d')
            vox_pred_follow, vox_logits_follow = tf.map_fn(decoder_reuse, tf.stack(aggr_feat_list_[1:]),
                dtype=(tf.float32, tf.float32))
            self.vox_pred = tf.stack([vox_pred_first]+tf.unstack(vox_pred_follow), axis=1)
            self.vox_list_logits = tf.stack([vox_logits_first]+tf.unstack(vox_logits_follow), axis=1)
            ## --------------- train -------------------
            ## --------------- test  -------------------
            reshape_size_test = [1, self.FLAGS.max_episode_length,
                4, 4, 4, self.fuse_feat_test.get_shape().as_list()[2]/64]
            aggr_feat_list_test_ = tf.unstack(tf.reshape(self.fuse_feat_test, reshape_size_test), axis=1)
            self.vox_pred_test, self.vox_logits_test = tf.map_fn(decoder_reuse_test, tf.stack(aggr_feat_list_test_),
                dtype=(tf.float32, tf.float32))
            self.vox_pred_test = tf.squeeze(tf.stack(tf.unstack(self.vox_pred_test), axis=1))
            self.vox_list_test_logits = tf.stack(tf.unstack(self.vox_logits_test), axis=1)
            ## --------------- test  -------------------

            ## TODO: create active agent with two stream policy network
            
        if self.FLAGS.debug_mode:
            summ.histogram('aggregated', self.vox_feat_list)
            summ.histogram('unet_out', self.vox_pred)
            
        ## create active agent
        with tf.device('/gpu:0'):
            ## extract input from list [BS, EP, ...] to [BS, EP-1, ...] as we do not use episode end to train
            ## --------------- train -------------------
            self.RGB_list_batch_norm_use, _ = tf.split(self.RGB_list_batch_norm, 
                [self.FLAGS.max_episode_length-1, 1], axis=1)
            self.aggr_feat_list_use, _ = tf.split(self.aggr_feat_list,
                [self.FLAGS.max_episode_length-1, 1], axis=1)
            #self.aggr_feat_list_use = tf.stack(self.aggr_feat_list[:-1], axis=1)
            ## collapse input for easy inference instead of inference multiple times
            self.RGB_use_batch = collapse_dims(self.RGB_list_batch_norm_use)
            self.aggr_feat_use = collapse_dims(self.aggr_feat_list_use)
            self.action_prob, _ = self._create_dqn_two_stream(self.RGB_use_batch, self.aggr_feat_use,
                trainable=True, if_bn=self.FLAGS.if_bn, scope_name='dqn_two_stream')

            ## --------------- train -------------------
            ## --------------- test  -------------------
            self.RGB_list_test_norm_use, _ = tf.split(self.RGB_list_test_norm,
                [self.FLAGS.max_episode_length-1, 1], axis=1)
            self.aggr_feat_list_use_test, _ = tf.split(self.aggr_feat_list_test,
                [self.FLAGS.max_episode_length-1, 1], axis=1)
            ## collapse input for easy inference instead of inference multiple times
            self.RGB_use_test = collapse_dims(self.RGB_list_test_norm_use)
            self.aggr_feat_test_use = collapse_dims(self.aggr_feat_list_use_test)
            self.action_prob_test, _ = self._create_dqn_two_stream(self.RGB_use_test, self.aggr_feat_test_use,
                trainable=False, if_bn=self.FLAGS.if_bn, reuse=True, scope_name='dqn_two_stream')
            ## --------------- test  -------------------
            ### TODO: debug
            #sys.exit()
            ### debug
    
    def _create_loss(self):
        ## create reconstruction loss
        ## --------------- train -------------------

        if not self.FLAGS.use_coef:
            recon_loss_mat = tf.nn.sigmoid_cross_entropy_with_logits(
                labels=self.vox_list_batch, 
                logits=self.vox_list_logits,
                name='recon_loss_mat',
            )
        else:
            recon_loss_mat = tf.nn.weighted_cross_entropy_with_logits(
                targets=self.vox_list_batch, 
                logits=self.vox_list_logits,
                pos_weight=self.FLAGS.loss_coef,
                name='recon_loss_mat',
            )
            

        self.recon_loss_list = tf.reduce_mean(
            recon_loss_mat,
            axis=[2, 3, 4, 5],
            name='recon_loss_list'
        ) ## [BS, EP, V, V, V, 1]
        
        self.recon_loss = tf.reduce_sum(self.recon_loss_list, axis=[0, 1], name='recon_loss')
        self.recon_loss_last = tf.reduce_sum(self.recon_loss_list[:, -1], axis=0, name='recon_loss_last')
        ## --------------- train -------------------
        ## --------------- test  -------------------

        if not self.FLAGS.use_coef:
            recon_loss_mat_test = tf.nn.sigmoid_cross_entropy_with_logits(
                labels=self.vox_list_test, 
                logits=self.vox_list_test_logits,
                name='recon_loss_mat',
            )
        else:
            recon_loss_mat_test = tf.nn.weighted_cross_entropy_with_logits(
                targets=self.vox_list_test, 
                logits=self.vox_list_test_logits,
                pos_weight=self.FLAGS.loss_coef,
                name='recon_loss_mat',
            )
        
        self.recon_loss_list_test = tf.reduce_mean(
            recon_loss_mat_test,
            axis=[2,3,4,5],
            name='recon_loss_list_test'
        )
        
        self.recon_loss_test = tf.reduce_sum(self.recon_loss_list_test, name='recon_loss_test')
        ## --------------- test  -------------------


        def process_loss_to_reward(loss_list_batch, gamma, max_episode_len, r_name=None, reward_weight=10):
            
            reward_raw_batch = loss_list_batch[:, :-1]-loss_list_batch[:, 1:]
            reward_batch_list = tf.get_variable(name='reward_batch_list_{}'.format(r_name), shape=reward_raw_batch.get_shape(),
                dtype=tf.float32, initializer=tf.zeros_initializer)

            batch_size = loss_list_batch.get_shape().as_list()[0]
            
            ## decayed sum of future possible rewards
            for i in range(max_episode_len):
                for j in range(i, max_episode_len):
                    update_r = reward_raw_batch[:, j]/tf.abs(loss_list_batch[:, j])*(gamma**(j-i))
                    update_r = update_r + reward_batch_list[:, i] 
                    update_r = tf.expand_dims(update_r, axis=1)
                    ## update reward batch list
                    reward_batch_list = tf.concat(axis=1, values=[reward_batch_list[:, :i], update_r,
                        reward_batch_list[:,i+1:]])

            return reward_weight*reward_batch_list, reward_weight*reward_raw_batch

        self.reward_batch_list, self.reward_raw_batch = process_loss_to_reward(
            self.recon_loss_list,
            self.FLAGS.gamma,
            self.FLAGS.max_episode_length-1,
            r_name=None,
            reward_weight=self.FLAGS.reward_weight
        )
        
        self.reward_test_list, self.reward_raw_test = process_loss_to_reward(
            self.recon_loss_list_test,
            self.FLAGS.gamma,
            self.FLAGS.max_episode_length-1,
            r_name='test',
            reward_weight=self.FLAGS.reward_weight
        )
            
        ## create reinforce loss
        self.action_batch = collapse_dims(self.action_list_batch)
        self.indexes = tf.range(0, tf.shape(self.action_prob)[0]) * tf.shape(self.action_prob)[1] + tf.reshape(self.action_batch, [-1])
        self.responsible_action = tf.gather(tf.reshape(self.action_prob, [-1]), self.indexes)
        ## reward_batch node should not back propagate
        self.reward_batch = tf.stop_gradient(collapse_dims(self.reward_batch_list), name='reward_batch')
        self.loss_reinforce = -tf.reduce_mean(tf.log(tf.clip_by_value(self.responsible_action, 1e-10, 1))*self.reward_batch, name='reinforce_loss')
        self.loss_act_regu = tf.reduce_sum(tf.clip_by_value(self.action_prob, 1e-10, 1)*tf.log(tf.clip_by_value(self.action_prob, 1e-10, 1)))  

    def _create_optimizer(self):
       
        aggr_var = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='aggr')
        enc_var = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='encoder')
        dec_var = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='decoder')
        fuse_var = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='fuse')
        dqn_var = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='dqn')

        if self.FLAGS.if_constantLr:
            self.learning_rate = self.FLAGS.learning_rate
            #self._log_string(tf_util.toGreen('===== Using constant lr!'))
        else:  
            self.learning_rate = get_learning_rate(self.counter, self.FLAGS)

        if self.FLAGS.optimizer == 'momentum':
            self.optimizer = tf.train.MomentumOptimizer(self.learning_rate, momentum=self.FLAGS.momentum)
        elif self.FLAGS.optimizer == 'adam':
            self.optimizer = tf.train.AdamOptimizer(self.learning_rate)

        #self.opt_recon = self.optimizer.minimize(self.recon_loss, var_list=aggr_var+unet_var, global_step=self.counter)  
        #self.opt_reinforce = self.optimizer.minimize(self.loss_reinforce, var_list=aggr_var+dqn_var,
        #    global_step=self.counter)

        #so that we have always have something to optimize
        self.recon_loss, z = other.tfutil.noop(self.recon_loss)
        
        #self.opt_recon = self.optimizer.minimize(self.recon_loss, var_list=fuse_var+aggr_var+enc_var+dec_var+[z])  
        self.opt_recon = slim.learning.create_train_op(self.recon_loss, optimizer=self.optimizer,
            variables_to_train=aggr_var+fuse_var+enc_var+dec_var)
        self.opt_recon_last = slim.learning.create_train_op(self.recon_loss_last, optimizer=self.optimizer, 
            variables_to_train=aggr_var+fuse_var+enc_var+dec_var)
        self.opt_reinforce = self.optimizer.minimize(self.loss_reinforce+self.FLAGS.reg_act*self.loss_act_regu, var_list=aggr_var+dqn_var)

    def _create_summary(self):
        #if self.FLAGS.is_training:
        self.summary_learning_rate = tf.summary.scalar('train/learning_rate', self.learning_rate)
        self.summary_loss_recon_train = tf.summary.scalar('train/loss_recon',
            self.recon_loss/(self.FLAGS.max_episode_length*self.FLAGS.batch_size))
        self.summary_loss_reinforce_train = tf.summary.scalar('train/loss_reinforce', self.loss_reinforce)
        self.summary_reward_batch_train = tf.summary.scalar('train/reward_batch', tf.reduce_sum(self.reward_batch))
        self.merged_train = tf.summary.merge_all()

    def _create_collections(self):
        dct_from_keys = lambda keys: {key: getattr(self, key) for key in keys}
        
        self.vox_prediction_collection = dict2obj(dct_from_keys(
            ['vox_pred_test', 'recon_loss_list_test', 'reward_raw_test', 'rotated_vox_test']
        ))

        burnin_list = [
            'recon_loss',
            'recon_loss_list',
            'action_prob',
            'reward_batch_list',
            'reward_raw_batch',
            'loss_reinforce',
        ]

        if self.FLAGS.burin_opt == 0:
            burnin_list += ['opt_recon']
        elif self.FLAGS.burin_opt == 1:
            burnin_list += ['opt_recon_last', 'recon_loss_last']

        print burnin_list

        train_list = burnin_list[:] + ['opt_reinforce', 'merged_train']
        train_mvnet_list = burnin_list[:] + ['merged_train']

        self.burnin_collection = dict2obj(dct_from_keys(burnin_list))
        self.train_collection = dict2obj(dct_from_keys(train_list))
        self.train_mvnet_collection = dict2obj(dct_from_keys(train_mvnet_list))
            
    def get_placeholders(self, include_vox, include_action, train_mode):
        
        placeholders = lambda: None
        if train_mode:
            placeholders.rgb = self.RGB_list_batch
            placeholders.invz = self.invZ_list_batch
            placeholders.mask = self.mask_list_batch
            placeholders.azimuth = self.azimuth_list_batch
            placeholders.elevation = self.elevation_list_batch

            if include_action:
                placeholders.action = self.action_list_batch
            if include_vox:
                placeholders.vox = self.vox_batch

        else:
            placeholders.rgb = self.RGB_list_test
            placeholders.invz = self.invZ_list_test
            placeholders.mask = self.mask_list_test
            placeholders.azimuth = self.azimuth_list_test
            placeholders.elevation = self.elevation_list_test

            if include_action:
                placeholders.action = self.action_list_test
            if include_vox:
                placeholders.vox = self.vox_test

        return placeholders

    def construct_feed_dict(self, mvnet_inputs, include_vox, include_action, train_mode = True):

        placeholders = self.get_placeholders(include_vox, include_action, train_mode = train_mode)

        feed_dict = {self.is_training: train_mode}

        keys = ['rgb', 'invz', 'mask', 'azimuth', 'elevation']
        if include_vox:
            assert mvnet_inputs.vox is not None
            keys.append('vox')
        if include_action:
            assert mvnet_inputs.action is not None
            keys.append('action')
            
        for key in keys:
            feed_dict[getattr(placeholders, key)] = getattr(mvnet_inputs, key)

        return feed_dict

    def run_collection_with_fd(self, obj, fd):
        dct = obj2dict(obj)
        outputs = self.sess.run(dct, feed_dict = fd)
        obj = dict2obj(outputs)
        return obj

    def select_action(self, mvnet_input, idx, is_training = False):
        
        feed_dict = self.construct_feed_dict(
            mvnet_input, include_vox = False, include_action = False, train_mode = False
        )
    
        #if np.random.uniform(low=0.0, high=1.0) > epsilon:
        #    action_prob = self.sess.run([self.action_prob], feed_dict=feed_dict)
        #else:
        #    return np.random.randint(low=0, high=FLAGS.action_num)
        stuff = self.sess.run([self.action_prob_test], feed_dict=feed_dict)
        action_prob = stuff[0][idx]
        if is_training:
            print(action_prob)
            a_response = np.random.choice(action_prob, p=action_prob)

            a_idx = np.argmax(action_prob == a_response)
            print(a_idx)
        else:
            print(action_prob)

            a_idx = np.argmax(action_prob)
            print(a_idx)
        return a_idx

    def predict_vox_list(self, mvnet_input, is_training = False):

        feed_dict = self.construct_feed_dict(
            mvnet_input, include_vox = True, include_action = False, train_mode = is_training
        )
        return self.run_collection_with_fd(self.vox_prediction_collection, feed_dict)

    def run_step(self, mvnet_input, mode, is_training = True):
        '''mode is one of ['burnin', 'train'] '''
        feed_dict = self.construct_feed_dict(
            mvnet_input, include_vox = True, include_action = True, train_mode = is_training
        )

        if mode == 'burnin':
            collection_to_run = self.burnin_collection
        elif mode == 'train':
            collection_to_run = self.train_collection
        elif mode == 'train_mv':
            collection_to_run = self.train_mvnet_collection

        return self.run_collection_with_fd(collection_to_run, feed_dict)

def obj2dict(obj):
    return obj.__dict__

def dict2obj(dct):
    x = lambda: None
    for key, val in dct.items():
        setattr(x, key, val)
    return x
    
class SingleInputFactory(object):
    def __init__(self, mem):
        self.mem = mem

    def make(self, azimuth, elevation, model_id, action = None):
        rgb, mask = self.mem.read_png_to_uint8(azimuth, elevation, model_id)
        invz = self.mem.read_invZ(azimuth, elevation, model_id)
        mask = (mask > 0.5).astype(np.float32) * (invz >= 1e-6)

        invz = invz[..., None]
        mask = mask[..., None]
        azimuth = azimuth[..., None]
        elevation = elevation[..., None]
        
        single_input = SingleInput(rgb, invz, mask, azimuth, elevation, action = action)
        return single_input
    
class SingleInput(object): 
    def __init__(self, rgb, invz, mask, azimuth, elevation, vox = None, action = None):
        self.rgb = rgb
        self.invz = invz
        self.mask = mask
        self.azimuth = azimuth
        self.elevation = elevation
        self.vox = vox
        self.action = action

class ShapeProvider(object):
    def __init__(self, FLAGS, batch_size = None):
        self.BS = FLAGS.batch_size if (batch_size is None) else batch_size
        
        self.make_shape = lambda x: (self.BS, FLAGS.max_episode_length) + x

        self.rgb_shape = self.make_shape((FLAGS.resolution, FLAGS.resolution, 3))
        self.invz_shape = self.make_shape((FLAGS.resolution, FLAGS.resolution, 1))
        self.mask_shape = self.make_shape((FLAGS.resolution, FLAGS.resolution, 1))
        self.vox_shape = (self.BS, FLAGS.voxel_resolution, FLAGS.voxel_resolution, FLAGS.voxel_resolution)
        
        self.azimuth_shape = self.make_shape((1,))
        self.elevation_shape = self.make_shape((1,))
        self.action_shape = (self.BS, FLAGS.max_episode_length-1, 1)

        self.dtypes = {
            'rgb': np.float32,
            'invz': np.float32,
            'mask': np.float32,
            'vox': np.float32,
            'azimuth': np.float32,
            'elevation': np.float32,
            'action': np.int32,
        }

    def make_np_zeros(self, dest = None, suffix = '_np'):
        if dest is None:
            dest = self
        for key in ['rgb', 'invz', 'mask', 'vox', 'azimuth', 'elevation', 'action']:
            arr = np.zeros(getattr(self, key+'_shape'), dtype = self.dtypes[key])
            setattr(dest, key+suffix, arr)

    def make_tf_ph(self, dest = None, suffix = '_ph'):
        if dest is None:
            dest = self
        for key in ['rgb', 'invz', 'mask', 'vox', 'azimuth', 'elevation', 'action']:
            ph = tf.placeholder(shape = getattr(self, key+'_shape'), dtype = self.dtypes[key])
            setattr(self, key+suffix, ph)
        
class MVInputs(object):
    def __init__(self, FLAGS, batch_size = None):

        self.FLAGS = FLAGS
        self.BS = FLAGS.batch_size if (batch_size is None) else batch_size

        self.provider = ShapeProvider(FLAGS, batch_size = batch_size)
        self.provider.make_np_zeros(dest = self, suffix = '')

    def put_voxel(self, voxel, batch_idx = 0):
        assert 0 <= batch_idx < self.BS
        self.vox[batch_idx, ...] = voxel
        
    def put(self, single_mvinput, episode_idx, batch_idx = 0):
        assert 0 <= batch_idx < self.BS
        assert 0 <= episode_idx < self.FLAGS.max_episode_length

        keys = ['rgb', 'invz', 'mask', 'azimuth', 'elevation']
        if hasattr(single_mvinput, 'action') and getattr(single_mvinput, 'action') is not None:
            keys.append('action')
            
        for key in keys:
            arr = getattr(self, key)
            arr[batch_idx, episode_idx, ...] = getattr(single_mvinput, key)
