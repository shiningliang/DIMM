import tensorflow as tf
import tensorflow.contrib as tc
import time
from .rnn_module import cu_rnn, nor_rnn
from .nn_module import dense, seq_loss, focal_loss, point_loss, multihead_attention, feedforward, label_smoothing


class sep_RNN_Model(object):
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
        self.n_head = args.n_head
        self.n_block = args.n_block
        self.n_label = args.n_class
        self.is_map = args.is_map
        self.is_att = args.ipt_att
        self.is_bi = args.is_bi
        self.is_point = args.is_point
        self.is_fc = args.is_fc
        self.opt_type = args.optim
        self.dropout_keep_prob = args.dropout_keep_prob
        self.weight_decay = args.weight_decay

        self.id, self.index, self.medicine, self.seq_len, self.org_len, self.labels = batch.get_next()
        self.N = tf.shape(self.id)[0]
        self.max_len = tf.reduce_max(self.seq_len)
        self.mask = tf.sequence_mask(self.seq_len, self.max_len, dtype=tf.float32, name='masks')
        self.index = tf.slice(self.index, [0, 0, 0], tf.stack([self.N, self.max_len, self.n_index]))
        self.medicine = tf.slice(self.medicine, [0, 0, 0], tf.stack([self.N, self.max_len, self.n_medicine]))
        # self.lr = tf.get_variable('lr', shape=[], dtype=tf.float32, trainable=False)
        self.is_train = tf.get_variable('is_train', shape=[], dtype=tf.bool, trainable=False)
        self.global_step = tf.get_variable('global_step', shape=[], dtype=tf.int32,
                                           initializer=tf.constant_initializer(0), trainable=False)
        self.lr = tf.train.exponential_decay(args.lr, global_step=self.global_step, decay_steps=args.checkpoint,
                                             decay_rate=0.96)
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
        self._index_encode()
        self._medicine_encode()
        self._rnn()
        # self._step_attention()
        if self.is_point:
            self._point_label()
        else:
            self._seq_label()
        self._compute_loss()
        self._create_train_op()
        self.logger.info('Time to build graph: {} s'.format(time.time() - start_t))

    def _index_encode(self):
        with tf.variable_scope('index_encoding', reuse=tf.AUTO_REUSE):
            if self.is_map:
                with tf.variable_scope('index', reuse=tf.AUTO_REUSE):
                    self.index = dense(self.index, hidden=self.n_hidden, initializer=self.initializer)
                    self.index = tf.reshape(self.index, [-1, self.max_len, self.n_hidden], name='2_3D')
            self.index, _ = cu_rnn('bi-gru', self.index, self.n_hidden, self.n_batch, self.is_train, self.n_layer)
            if self.is_att:
                self.index = self._input_attention(self.index, self.n_hidden if self.is_map else self.n_index,
                                                   'index_attention')
            if self.is_train:
                self.index = tf.nn.dropout(self.index, self.dropout_keep_prob)

    def _medicine_encode(self):
        with tf.variable_scope('medicine_encoding', reuse=tf.AUTO_REUSE):
            if self.is_map:
                with tf.variable_scope('medicine', reuse=tf.AUTO_REUSE):
                    self.medicine = dense(self.medicine, hidden=self.n_hidden, initializer=self.initializer)
                    self.medicine = tf.reshape(self.medicine, [-1, self.max_len, self.n_hidden], name='2_3D')
            self.medicine, _ = cu_rnn('bi-gru', self.medicine, self.n_hidden, self.n_batch, self.is_train, self.n_layer)
            if self.is_att:
                self.medicine = self._input_attention(self.medicine, self.n_hidden if self.is_map else self.n_index,
                                                      'medicine_attention')
            if self.is_train:
                self.medicine = tf.nn.dropout(self.medicine, self.dropout_keep_prob)

    def _input_attention(self, input_encodes, n_unit, scope):
        with tf.variable_scope(scope, reuse=tf.AUTO_REUSE):
            input_encodes = multihead_attention(input_encodes, input_encodes, n_unit, num_heads=1,
                                                dropout_rate=1 - self.dropout_keep_prob,
                                                is_training=self.is_train, reuse=tf.AUTO_REUSE,
                                                initializer=self.initializer)
            return input_encodes

    def _rnn(self):
        with tf.variable_scope('rnn', reuse=tf.AUTO_REUSE):
            self.input_encodes = tf.concat([self.index, self.medicine], 2)
            self.n_hidden = 2 * self.n_hidden * self.n_layer
            if self.use_cudnn:
                self.seq_encodes, _ = cu_rnn('bi-gru', self.input_encodes, self.n_hidden, self.n_batch,
                                             self.is_train, self.n_layer)
            else:
                self.seq_encodes = nor_rnn('bi-sru', self.input_encodes, self.seq_len, self.n_hidden,
                                           self.n_layer, self.dropout_keep_prob)
        if self.is_train:
            self.seq_encodes = tf.nn.dropout(self.seq_encodes, self.dropout_keep_prob)

    def _step_attention(self):
        with tf.variable_scope('step_attention', reuse=tf.AUTO_REUSE):
            for i in range(self.n_block):
                self.seq_encodes = multihead_attention(self.seq_encodes, self.seq_encodes, self.n_hidden,
                                                       num_heads=self.n_head, dropout_rate=1 - self.dropout_keep_prob,
                                                       is_training=self.is_train, reuse=tf.AUTO_REUSE,
                                                       initializer=self.initializer)
                # self.seq_encodes = feedforward(self.seq_encodes, [4*self.n_hidden, self.n_hidden],
                #                                initializer=self.initializer)

    def _seq_label(self):
        with tf.variable_scope('seq_labels', reuse=tf.AUTO_REUSE):
            self.seq_encodes = tf.reshape(self.seq_encodes, [-1, 2 * self.n_hidden])
            self.label_dense_1 = tf.nn.relu(dense(self.seq_encodes, hidden=int(self.n_hidden / 2), scope='dense_1',
                                                  initializer=self.initializer))
            if self.is_train:
                self.label_dense_1 = tf.nn.dropout(self.label_dense_1, self.dropout_keep_prob)
            self.outputs = dense(self.label_dense_1, hidden=self.n_label, scope='output_labels',
                                 initializer=self.initializer)
            self.outputs = tf.reshape(self.outputs, tf.stack([-1, self.max_len, self.n_label]))
            self.labels = tf.tile(tf.expand_dims(self.labels, axis=1), tf.stack([1, self.max_len]))

            if self.is_fc:
                self.label_loss = focal_loss(self.outputs, self.labels, self.mask)
            else:
                self.label_loss = seq_loss(self.outputs, self.labels, self.mask)

    def _point_label(self):
        with tf.variable_scope('point_labels', reuse=tf.AUTO_REUSE):
            # self.seq_encodes = tf.squeeze(tf.gather_nd(self.seq_encodes, tf.stack(
            #     [tf.range(self.seq_len.shape[0])[..., tf.newaxis], self.seq_len[..., tf.newaxis]], axis=2)))
            self.last_encodes = self.seq_encodes[:, -1, :]
            self.label_dense_1 = tf.nn.relu(dense(self.last_encodes, hidden=int(self.n_hidden / 2), scope='dense_1',
                                                  initializer=self.initializer))
            if self.is_train:
                self.label_dense_1 = tf.nn.dropout(self.label_dense_1, self.dropout_keep_prob)
            self.outputs = dense(self.label_dense_1, hidden=self.n_label, scope='output_labels',
                                 initializer=self.initializer)

            self.label_loss = point_loss(self.outputs, self.labels)

    def _compute_loss(self):
        self.all_params = tf.trainable_variables()
        self.pre_labels = tf.argmax(self.outputs, axis=1 if self.is_point else 2)
        self.loss = self.label_loss
        if self.weight_decay > 0:
            with tf.variable_scope('l2_loss'):
                l2_loss = tf.add_n([tf.nn.l2_loss(v) for v in self.all_params])
            self.loss += self.weight_decay * l2_loss

    def _compute_acc(self):
        with tf.name_scope('accuracy'):
            correct_predictions = tf.equal(self.pre_labels, self.labels)
            self.accuracy = tf.reduce_mean(tf.cast(correct_predictions, 'float'), name='accuracy')

    def _create_train_op(self):
        """
        Selects the training algorithm and creates a train operation with it
        """
        with tf.variable_scope('optimizer', reuse=tf.AUTO_REUSE):
            if self.opt_type == 'adagrad':
                self.optimizer = tf.train.AdagradOptimizer(self.lr)
            elif self.opt_type == 'adam':
                self.optimizer = tf.train.AdamOptimizer(self.lr)
            elif self.opt_type == 'rprop':
                self.optimizer = tf.train.RMSPropOptimizer(self.lr)
            elif self.opt_type == 'sgd':
                self.optimizer = tf.train.GradientDescentOptimizer(self.lr)
            else:
                raise NotImplementedError('Unsupported optimizer: {}'.format(self.opt_type))
            self.grads, _ = tf.clip_by_global_norm(tf.gradients(self.loss, self.all_params), 25)
            self.train_op = self.optimizer.apply_gradients(zip(self.grads, self.all_params),
                                                           global_step=self.global_step)
            # self.train_op = self.optimizer.minimize(self.loss, global_step=self.global_step)
