import tensorflow as tf
import tensorflow.keras.mixed_precision as prec
from tensorflow_probability import distributions as tfd

import models
import networks
import tools


class Random(tools.Module):

  def __init__(self, config):


    self._config = config
    self._float = prec.global_policy().compute_dtype

  def actor(self, feat, *args):
    shape = feat.shape[:-1] + [self._config.num_actions]
    if self._config.actor_dist == 'onehot':
      return tools.OneHotDist(tf.zeros(shape))
    else:
      ones = tf.ones(shape, self._float)
      return tfd.Uniform(-ones, ones)

  def train(self, start, feat, embed, kl):
    return None, {}


class Plan2Explore(tools.Module):

  def __init__(self, config, world_model, reward=None):

    self._config = config
    self._reward = reward
    self._behavior = models.ImagBehavior(config, world_model)
    self.actor = self._behavior.actor
    size = {
        'embed': 32 * config.cnn_depth,
        'stoch': config.dyn_stoch,
        'deter': config.dyn_deter,
        'feat': config.dyn_stoch + config.dyn_deter,
    }[self._config.disag_target]
    kw = dict(
        shape=size, layers=config.disag_layers, units=config.disag_units,
        act=config.act)
    self._networks = [
        networks.DenseHead(**kw) for _ in range(config.disag_models)]
    self._opt = tools.Optimizer(
        'ensemble', config.model_lr, config.opt_eps, config.grad_clip,
        config.weight_decay, opt=config.opt)

  def train(self, start, feat, embed, kl):
    metrics = {}
    target = {
        'embed': embed,
        'stoch': start['stoch'],
        'deter': start['deter'],
        'feat': feat,
    }[self._config.disag_target]
    metrics.update(self._train_ensemble(feat, target))
    metrics.update(self._behavior.train(start, self._intrinsic_reward)[-1])
    return None, metrics

  def _intrinsic_reward(self, feat, state, action):
    preds = [head(feat, tf.float32).mean() for head in self._networks]
    disag = tf.reduce_mean(tf.math.reduce_std(preds, 0), -1)
    if self._config.disag_log:
      disag = tf.math.log(disag)
    reward = self._config.expl_intr_scale * disag
    if self._config.expl_extr_scale:
      reward += tf.cast(self._config.expl_extr_scale * self._reward(
          feat, state, action), tf.float32)
    return reward

  def _train_ensemble(self, inputs, targets):

    if self._config.disag_offset:
      targets = targets[:, self._config.disag_offset:]
      inputs = inputs[:, :-self._config.disag_offset]
    targets = tf.stop_gradient(targets)
    inputs = tf.stop_gradient(inputs)
    with tf.GradientTape() as tape:
      preds = [head(inputs) for head in self._networks]
      likes = [tf.reduce_mean(pred.log_prob(targets)) for pred in preds]
      loss = -tf.cast(tf.reduce_sum(likes), tf.float32)
    metrics = self._opt(tape, loss, self._networks)
    return metrics

  def act(self, feat, *args):
    return self.actor(feat)
