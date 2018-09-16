import tensorflow as tf
import tensorflow.contrib as tc
import time
from .rnn_module import cu_rnn, nor_rnn
from .nn_module import dense, seq_loss, focal_loss, point_loss, label_smoothing
from .attention_module import self_transformer


class Joint_DIMM_Model(object):
    def __init__(self, args, batch, dim, logger):
        # logging
        self.logger = logger
        # basic config
        self.n_index = dim[0]
        self.n_medicine = dim[1]
        self.n_hidden = args.n_hidden
        self.use_cudnn = args.use_cudnn
        self.n_batch = tf.get_variable('n_batch', shape=[], dtype=tf.int32, trainable=False)
        self.n_layer = args.n_layer
        self.n_mor = args.n_mortality
        self.n_dis = args.n_disease
        self.is_map = args.is_map
        self.ipt_att = args.ipt_att
        self.block_ipt = args.block_ipt
        self.head_ipt = args.head_ipt
        self.step_att = args.step_att
        self.block_stp = args.block_stp
        self.head_stp = args.head_stp
        self.is_bi = args.is_bi
        self.is_point = args.is_point
        self.is_fc = args.is_fc
        self.alpha = args.alpha
        self.opt_type = args.optim
        self.dropout_keep_prob = args.dropout_keep_prob
        self.weight_decay = args.weight_decay

        self.id, self.index, self.medicine, self.seq_len, self.mor_labels, self.dis_labels = batch.get_next()
        self.N = tf.shape(self.id)[0]
        self.max_len = tf.reduce_max(self.seq_len)
        self.mask = tf.sequence_mask(self.seq_len, self.max_len, dtype=tf.float32, name='masks')
        self.padding = tf.sequence_mask(self.seq_len, self.max_len, dtype=tf.int32, name='padding')
        self.index = tf.slice(self.index, [0, 0, 0], tf.stack([self.N, self.max_len, self.n_index]))
        self.medicine = tf.slice(self.medicine, [0, 0, 0], tf.stack([self.N, self.max_len, self.n_medicine]))
        self.lr = tf.get_variable('lr', shape=[], dtype=tf.float32, trainable=False)
        self.is_train = tf.get_variable('is_train', shape=[], dtype=tf.bool, trainable=False)
        self.global_step = tf.get_variable('global_step', shape=[], dtype=tf.int32,
                                           initializer=tf.constant_initializer(0), trainable=False)
        # self.lr = tf.train.exponential_decay(args.lr, global_step=self.global_step, decay_steps=args.checkpoint,
        #                                      decay_rate=0.96)
        self.initializer = tc.layers.xavier_initializer()

        self._build_graph()
        # if self.is_train:
        #     # save info
        #     self.saver = tf.train.Saver()
        # else:
        #     self.saver = model_saver

        # initialize the model
        # self.sess.run(tf.global_variables_initializer())

    def _build_graph(self):
        start_t = time.time()
        self._encode()
        self._rnn()
        if self.step_att:
            self._step_attention()
        if self.is_point:
            self._point_mortality()
        else:
            self._seq_mortality()
        self._disease_classification()
        self._compute_loss()
        self._create_train_op()
        self.logger.info('Time to build graph: {} s'.format(time.time() - start_t))

    def _encode(self):
        with tf.variable_scope('input_encoding', reuse=tf.AUTO_REUSE):
            if self.is_map:
                with tf.variable_scope('index', reuse=tf.AUTO_REUSE):
                    self.index = dense(self.index, hidden=self.n_hidden, initializer=self.initializer)
                    self.index = tf.reshape(self.index, [-1, self.max_len, self.n_hidden], name='2_3D')
                with tf.variable_scope('medicine', reuse=tf.AUTO_REUSE):
                    self.medicine = dense(self.medicine, hidden=self.n_hidden, initializer=self.initializer)
                    self.medicine = tf.reshape(self.medicine, [-1, self.max_len, self.n_hidden], name='2_3D')
            if self.ipt_att:
                self.index = self._input_attention(self.index, self.index,
                                                   self.n_hidden if self.is_map else self.n_index,
                                                   'i2i_attention')
                self.i2m = self._input_attention(self.index, self.medicine,
                                                 self.n_hidden if self.is_map else self.n_index,
                                                 'i2m_attention')
                self.medicine = self._input_attention(self.medicine, self.medicine,
                                                      self.n_hidden if self.is_map else self.n_medicine,
                                                      'm2m_attention')
                self.m2i = self._input_attention(self.medicine, self.index,
                                                 self.n_hidden if self.is_map else self.n_medicine,
                                                 'm2i_attention')
            self.input_encodes = tf.concat([self.index, self.medicine, self.i2m, self.m2i], 2)
            if self.is_train:
                self.input_encodes = tf.nn.dropout(self.input_encodes, self.dropout_keep_prob)

    def _input_attention(self, input_x, input_y, n_unit, scope):
        with tf.variable_scope(scope, reuse=tf.AUTO_REUSE):
            input_encodes = self_transformer(input_x, input_y, self.mask, self.block_ipt, n_unit, self.head_ipt,
                                             self.dropout_keep_prob, False, self.is_train)
            return input_encodes

    def _rnn(self):
        with tf.variable_scope('rnn', reuse=tf.AUTO_REUSE):
            if self.use_cudnn:
                self.seq_encodes, self.seq_states = cu_rnn('bi-gru', self.input_encodes, self.n_hidden, self.n_batch,
                                                           self.is_train, self.n_layer)
            else:
                self.seq_encodes = nor_rnn('bi-sru', self.input_encodes, self.seq_len, self.n_hidden,
                                           self.n_layer, self.dropout_keep_prob)
        if self.is_bi:
            self.n_hidden *= self.n_layer
        if self.is_train:
            self.seq_encodes = tf.nn.dropout(self.seq_encodes, self.dropout_keep_prob)

    def _step_attention(self):
        with tf.variable_scope('step_attention', reuse=tf.AUTO_REUSE):
            self.seq_encodes = self_transformer(self.seq_encodes, self.seq_encodes, self.mask, self.block_stp,
                                                self.n_hidden, self.head_stp, self.dropout_keep_prob,
                                                True, self.is_train)

    def _seq_mortality(self):
        with tf.variable_scope('seq_mortality', reuse=tf.AUTO_REUSE):
            self.seq_encodes = tf.reshape(self.seq_encodes, [-1, self.n_hidden])
            self.outputs_mor = dense(self.seq_encodes, hidden=self.n_mor, scope='output_mortality',
                                     initializer=self.initializer)
            self.outputs_mor = tf.reshape(self.outputs_mor, tf.stack([-1, self.max_len, self.n_mor]))
            self.mor_labels = tf.tile(tf.expand_dims(self.mor_labels, axis=1), tf.stack([1, self.max_len]))
            if self.is_fc:
                self.mor_loss = focal_loss(self.outputs_mor, self.mor_labels, self.mask)
            else:
                self.mor_loss = seq_loss(self.outputs_mor, self.mor_labels, self.mask)

    def _point_mortality(self):
        with tf.variable_scope('point_mortality', reuse=tf.AUTO_REUSE):
            self.last_encodes = self.seq_encodes[:, -1, :]
            self.label_dense_1 = tf.nn.relu(dense(self.last_encodes, hidden=int(self.n_hidden / 2), scope='dense',
                                                  initializer=self.initializer))
            if self.is_train:
                self.label_dense_1 = tf.nn.dropout(self.label_dense_1, self.dropout_keep_prob)
            self.outputs_mor = dense(self.label_dense_1, hidden=self.n_mor, scope='output_mortality',
                                     initializer=self.initializer)
            self.mor_loss = point_loss(self.outputs_mor, self.mor_labels)

    def _disease_classification(self):
        with tf.variable_scope('disease_classification', reuse=tf.AUTO_REUSE):
            self.seq_states = self.seq_states[0]
            n_dir = self.seq_states.shape.as_list()[0]
            self.seq_states = tf.reshape(self.seq_states, [-1, int(n_dir * self.n_hidden / self.n_layer)])
            self.outputs_dis = dense(self.seq_states, hidden=self.n_dis, scope='output_disease',
                                     initializer=self.initializer)
            self.dis_loss = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits_v2(logits=self.outputs_dis,
                                                                                      labels=tf.stop_gradient(
                                                                                          tf.one_hot(self.dis_labels, 2))))

    def _compute_loss(self):
        self.all_params = tf.trainable_variables()
        self.mor_preds = tf.argmax(self.outputs_mor, axis=1 if self.is_point else 2)
        self.dis_preds = tf.argmax(self.outputs_dis, axis=1)
        self.loss = self.mor_loss + self.dis_loss
        if self.weight_decay > 0:
            with tf.variable_scope('l2_loss'):
                l2_loss = tf.add_n([tf.nn.l2_loss(v) for v in self.all_params])
            self.loss += self.weight_decay * l2_loss

    def _create_train_op(self):
        with tf.variable_scope('optimizer', reuse=tf.AUTO_REUSE):
            if self.opt_type == 'adagrad':
                self.optimizer = tf.train.AdagradOptimizer(self.lr)
            elif self.opt_type == 'adam':
                self.optimizer = tc.opt.LazyAdamOptimizer(self.lr)
                # self.optimizer = tc.opt.AdamWOptimizer(self.weight_decay, self.lr)
            elif self.opt_type == 'rprop':
                self.optimizer = tf.train.RMSPropOptimizer(self.lr)
            elif self.opt_type == 'sgd':
                self.optimizer = tf.train.GradientDescentOptimizer(self.lr)
            else:
                raise NotImplementedError('Unsupported optimizer: {}'.format(self.opt_type))
            self.grads, _ = tf.clip_by_global_norm(tf.gradients(self.loss, self.all_params), 25)
            self.train_op = self.optimizer.apply_gradients(zip(self.grads, self.all_params),
                                                           global_step=self.global_step)