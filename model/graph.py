from model.embedding import Embedding
from model.loss import sequence_loss
from model.metric import Metric
from model.optimizer import TransformerOptimizer
from util import constant

import tensorflow as tf


class Graph:
    def __init__(self, data, is_train, model_config):
        self.model_config = model_config
        self.data = data
        self.is_train = is_train
        # model_fn defines core computational graph
        # decoder_outputs, logits, target_outputs
        # decoder_outputs is [batch * length * dimension]
        # logits is [batch * length * vocab_size]
        # target_outputs is [batch * length * vocab_size]
        # in training, target_outputs is gt target
        # in eval, target_outputs is output target
        self.model_fn = None
        print('Batch Size:\t%d.' % self.model_config.batch_size)
        self.rand_unif_init = tf.random_uniform_initializer(-0,.08, 0.08)
        self.metric = Metric(self.model_config, self.data)

    def embedding_fn(self, inputs, embedding):
        # with tf.device(self.device_config):
        if not inputs:
            return []
        else:
            return [tf.nn.embedding_lookup(embedding, inp) for inp in inputs]

    def create_model_multigpu(self):
        # with tf.Graph().as_default():
            with tf.device('/cpu:0'):
                losses = []
                grads = []
                self.objs = []
                optim = self.get_optim()
                for gpu_id in range(self.model_config.num_gpus):
                    with tf.device('/gpu:%d' % gpu_id):
                        loss, obj = self.create_model()
                        grad = optim.compute_gradients(loss)
                        losses.append(loss)
                        grads.append(grad)
                        self.objs.append(obj)
                        tf.get_variable_scope().reuse_variables()

                with tf.variable_scope('optimization', reuse=tf.AUTO_REUSE):

                    self.loss = tf.divide(tf.add_n(losses), self.model_config.num_gpus)
                    self.perplexity = tf.exp(tf.reduce_mean(self.loss))

                    self.global_step = tf.get_variable(
                        'global_step', initializer=tf.constant(0, dtype=tf.int64), trainable=False)

                    if self.is_train:
                        avg_grad = self.average_gradients(grads)
                        grads = [g for (g,v) in avg_grad]
                        clipped_grads, _ = tf.clip_by_global_norm(grads, self.model_config.max_grad_norm)
                        self.train_op = optim.apply_gradients(zip(clipped_grads, tf.trainable_variables()), global_step=self.global_step)

                        self.increment_global_step = tf.assign_add(self.global_step, 1)

                    self.saver = tf.train.Saver(write_version=tf.train.SaverDef.V2)

    def create_model(self):
        with tf.variable_scope('variables'):
            sentence_simple_input_placeholder = []
            for step in range(self.model_config.max_simple_sentence):
                sentence_simple_input_placeholder.append(
                    tf.zeros(self.model_config.batch_size, tf.int32, name='simple_input'))

            sentence_simple_input_prior_placeholder = []
            for step in range(self.model_config.max_simple_sentence):
                sentence_simple_input_prior_placeholder.append(
                    tf.ones(self.model_config.batch_size, tf.float32, name='simple_input_prior'))

            sentence_complex_input_placeholder = []
            for step in range(self.model_config.max_complex_sentence):
                sentence_complex_input_placeholder.append(
                    tf.zeros(self.model_config.batch_size, tf.int32, name='complex_input'))

            sentence_idxs = tf.zeros(self.model_config.batch_size, tf.int32, name='complex_input')

            embedding = Embedding(self.data.vocab_complex, self.data.vocab_simple, self.model_config)
            emb_complex = embedding.get_complex_embedding()
            emb_simple = embedding.get_simple_embedding()
            # if (self.is_train and self.model_config.pretrained_embedding is not None and
            #             self.model_config.subword_vocab_size <= 0):
            #     embed_complex_placeholder = tf.placeholder(
            #         tf.float32, (self.data.vocab_complex.vocab_size(), self.model_config.dimension),
            #         'complex_emb')
            #     replace_emb_complex = emb_complex.assign(embed_complex_placeholder)
            #
            #     embed_simple_placeholder = tf.placeholder(
            #         tf.float32, (self.data.vocab_simple.vocab_size(), self.model_config.dimension),
            #         'simple_emb')
            #     replace_emb_simple = emb_simple.assign(embed_simple_placeholder)

            w = embedding.get_w()
            b = embedding.get_b()

        with tf.variable_scope('model'):
            output = self.model_fn(sentence_complex_input_placeholder, emb_complex,
                       sentence_simple_input_placeholder, emb_simple,
                       w, b)

            if not self.is_train and self.model_config.replace_unk_by_emb:
                # Get output list matrix for replacement by embedding
                # self.encoder_embs = tf.stack(
                #     self.embedding_fn(self.sentence_complex_input_placeholder, self.emb_complex),
                #     axis=1)
                encoder_embs = tf.stack(output.encoder_embed_inputs_list, axis=1)
                # self.encoder_embs = output.encoder_outputs
                if type(output.decoder_outputs) == list:
                    decoder_outputs = tf.stack(output.decoder_outputs, axis=1)
                else:
                    decoder_outputs = output.decoder_outputs

            if not self.is_train:
                # in beam search, it directly provide decoder target list
                decoder_target_list = tf.stack(output.decoder_target_list, axis=1)
                loss = tf.reduce_mean(output.decoder_score)
                return loss, \
                       {
                           'sentence_idxs': sentence_idxs,
                           'sentence_simple_input_placeholder': sentence_simple_input_placeholder,
                           'sentence_complex_input_placeholder': sentence_complex_input_placeholder,
                           'sentence_simple_input_prior_placeholder': sentence_simple_input_prior_placeholder,
                           'decoder_target_list': decoder_target_list,
                           'decoder_outputs':decoder_outputs,
                           'encoder_embs':encoder_embs
                       }
            else:
                decode_word_weight = tf.stack(
                    [tf.to_float(tf.not_equal(d, self.data.vocab_simple.encode(constant.SYMBOL_PAD)))
                     for d in output.gt_target_list], axis=1)

                prior_weight = tf.stack(sentence_simple_input_prior_placeholder, axis=1)
                decode_word_weight = tf.multiply(prior_weight, decode_word_weight)

                gt_target = tf.stack(output.gt_target_list, axis=1)

                loss_fn = None

                loss = sequence_loss(logits=tf.stack(output.decoder_logit_list, axis=1),
                                          targets=gt_target,
                                          weights=decode_word_weight,
                                          softmax_loss_function=loss_fn)
                return loss, \
                       {
                           'sentence_idxs':sentence_idxs,
                           'sentence_simple_input_placeholder':sentence_simple_input_placeholder,
                           'sentence_complex_input_placeholder':sentence_complex_input_placeholder,
                           'sentence_simple_input_prior_placeholder':sentence_simple_input_prior_placeholder,
                       }

    def get_optim(self):
        learning_rate = tf.constant(self.model_config.learning_rate)

        if self.model_config.optimizer == 'adagrad':
            opt = tf.train.AdagradOptimizer(learning_rate)
        # Adam need lower learning rate
        elif self.model_config.optimizer == 'adam':
            opt = tf.train.AdamOptimizer(learning_rate)
        elif self.model_config.optimizer == 'lazy_adam':
            if not hasattr(self, 'hparams'):
                # In case not using Transformer model
                from tensor2tensor.models import transformer
                self.hparams = transformer.transformer_base()
            opt = tf.contrib.opt.LazyAdamOptimizer(
                self.hparams.learning_rate / 100.0,
                beta1=self.hparams.optimizer_adam_beta1,
                beta2=self.hparams.optimizer_adam_beta2,
                epsilon=self.hparams.optimizer_adam_epsilon)
        elif self.model_config.optimizer == 'adadelta':
            opt = tf.train.AdadeltaOptimizer(learning_rate)
        else:
            raise Exception('Not Implemented Optimizer!')

        # if self.model_config.max_grad_staleness > 0:
        #     opt = tf.contrib.opt.DropStaleGradientOptimizer(opt, self.model_config.max_grad_staleness)

        return opt

    # Got from https://github.com/tensorflow/models/blob/master/tutorials/image/cifar10/cifar10_multi_gpu_train.py#L101
    def average_gradients(self, tower_grads):
        """Calculate the average gradient for each shared variable across all towers.
        Note that this function provides a synchronization point across all towers.
        Args:
          tower_grads: List of lists of (gradient, variable) tuples. The outer list
            is over individual gradients. The inner list is over the gradient
            calculation for each tower.
        Returns:
           List of pairs of (gradient, variable) where the gradient has been averaged
           across all towers.
        """
        average_grads = []
        for grad_and_vars in zip(*tower_grads):
            # Note that each grad_and_vars looks like the following:
            #   ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
            grads = []
            for g, _ in grad_and_vars:
                # Add 0 dimension to the gradients to represent the tower.
                expanded_g = tf.expand_dims(g, 0)

                # Append on a 'tower' dimension which we will average over below.
                grads.append(expanded_g)

            # Average over the 'tower' dimension.
            grad = tf.concat(axis=0, values=grads)
            grad = tf.reduce_mean(grad, 0)

            # Keep in mind that the Variables are redundant because they are shared
            # across towers. So .. we will just return the first tower's pointer to
            # the Variable.
            v = grad_and_vars[0][1]
            grad_and_var = (grad, v)
            average_grads.append(grad_and_var)
        return average_grads

class ModelOutput:
    def __init__(self, decoder_outputs=None, decoder_logit_list=None, decoder_target_list=None,
                 decoder_score=None, gt_target_list=None, encoder_embed_inputs_list=None, encoder_outputs=None):
        self._decoder_outputs = decoder_outputs
        self._decoder_logit_list = decoder_logit_list
        self._decoder_target_list = decoder_target_list
        self._decoder_score = decoder_score
        self._gt_target_list = gt_target_list
        self._encoder_embed_inputs_list = encoder_embed_inputs_list
        self._encoder_outputs = encoder_outputs

    @property
    def encoder_outputs(self):
        return self._encoder_outputs

    @property
    def encoder_embed_inputs_list(self):
        """The final embedding input before model."""
        return self._encoder_embed_inputs_list

    @property
    def decoder_outputs(self):
        return self._decoder_outputs

    @property
    def decoder_logit_list(self):
        return self._decoder_logit_list

    @property
    def decoder_target_list(self):
        return self._decoder_target_list

    @property
    def decoder_score(self):
        return self._decoder_score

    @property
    def gt_target_list(self):
        return self._gt_target_list
