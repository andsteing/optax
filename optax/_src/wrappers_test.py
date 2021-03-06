# Lint as: python3
# Copyright 2019 DeepMind Technologies Limited. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Tests for `wrappers.py`."""

from absl.testing import absltest
from absl.testing import parameterized
import chex
import haiku as hk
import jax
import jax.numpy as jnp
import numpy as np
from optax._src import alias
from optax._src import transform
from optax._src import update
from optax._src import wrappers


def _build_sgd():
  return alias.sgd(1.)


def _build_simple_adam():
  # This adam behaves like an sgd, but with state.
  return alias.adam(1., b1=0., b2=0.)


class WrappersTest(parameterized.TestCase):

  def test_flatten(self):
    def init_params():
      return (jnp.array([1., 2.]), jnp.array([3., 4.]))

    per_step_updates = (jnp.array([500., 5.]), jnp.array([300., 3.]))

    # First calculate new params without flattening
    optax_sgd_params = init_params()
    sgd = alias.sgd(1e-2, 0.0)
    state_sgd = sgd.init(optax_sgd_params)
    updates_sgd, state_sgd = sgd.update(per_step_updates, state_sgd)
    sgd_params_no_flatten = update.apply_updates(optax_sgd_params, updates_sgd)

    # And now calculate new params with flattening
    optax_sgd_params = init_params()
    sgd = wrappers.flatten(sgd)
    state_sgd = sgd.init(optax_sgd_params)
    updates_sgd, state_sgd = sgd.update(per_step_updates, state_sgd)
    sgd_params_flatten = update.apply_updates(optax_sgd_params, updates_sgd)

    # Test that both give the same result
    chex.assert_tree_all_close(
        sgd_params_no_flatten, sgd_params_flatten, atol=1e-7, rtol=1e-7)

  @chex.variants(with_jit=True, without_jit=True, with_pmap=True)
  @parameterized.parameters([
      _build_sgd,
      _build_simple_adam,
  ])
  def test_apply_if_finite(self, opt_builder):
    one = jnp.ones([])
    nan = jnp.array(jnp.nan)
    def fn(x):
      return x * hk.get_parameter('p', [], init=hk.initializers.Constant(0.))

    fn = hk.without_apply_rng(hk.transform(fn))
    params = fn.init(jax.random.PRNGKey(1905), one)
    opt = wrappers.apply_if_finite(opt_builder(), 2)
    state = opt.init(params)
    grads_fn = jax.grad(self.variant(fn.apply))
    # Do one successful param update
    grads = grads_fn(params, one)
    updates, state = opt.update(grads, state, params)
    params = update.apply_updates(params, updates)
    # We know exactly what should be the value of params since we are
    # effectively using sgd in all cases.
    self.assertEqual(-1., float(jax.tree_flatten(params)[0][0]))
    self.assertTrue(bool(state.last_finite))
    # Check 2 rejected param updates
    for step in range(2):
      grads = grads_fn(params, nan)
      updates, state = opt.update(grads, state, params)
      params = update.apply_updates(params, updates)
      self.assertEqual(-1., float(jax.tree_flatten(params)[0][0]))
      self.assertFalse(bool(state.last_finite))
      self.assertEqual(step + 1, int(state.notfinite_count))
    # Next successful param update
    grads = grads_fn(params, one)
    updates, state = opt.update(grads, state, params)
    params = update.apply_updates(params, updates)
    self.assertEqual(-2., float(jax.tree_flatten(params)[0][0]))
    self.assertTrue(bool(state.last_finite))
    # Again 2 rejected param updates
    for step in range(2):
      grads = grads_fn(params, nan)
      updates, state = opt.update(grads, state, params)
      params = update.apply_updates(params, updates)
      self.assertEqual(-2., float(jax.tree_flatten(params)[0][0]))
      self.assertFalse(bool(state.last_finite))
      self.assertEqual(step + 1, int(state.notfinite_count))
    # Next param update with NaN is accepted since we reached maximum
    grads = grads_fn(params, nan)
    updates, state = opt.update(grads, state, params)
    params = update.apply_updates(params, updates)
    self.assertTrue(bool(jnp.isnan(jax.tree_flatten(params)[0][0])))
    self.assertEqual(5, int(state.total_notfinite))

  def test_apply_if_finite_pmap(self):
    # Unlike in `test_apply_if_finite`:
    # * pmap is applied to the gradient computation and the optimisation;
    # * the NaNs are caused inside the function and do not come from the inputs.
    half = jnp.ones([1]) / 2.
    two = jnp.ones([1]) * 2.  # Causes a NaN in arctanh
    def fn(x):
      return jnp.arctanh(x) * hk.get_parameter(
          'p', [], init=hk.initializers.Constant(0.))
    fn = hk.without_apply_rng(hk.transform(fn))

    opt = wrappers.apply_if_finite(alias.sgd(1.), 2)
    def fn_update(params, opt_state, x):
      grads = jax.grad(fn.apply)(params, x)
      grads = jax.lax.psum(grads, axis_name='i')
      updates, new_opt_state = opt.update(grads, opt_state, params)
      new_params = update.apply_updates(params, updates)
      return new_params, new_opt_state
    fn_update = jax.pmap(fn_update, axis_name='i')

    params = fn.init(jax.random.PRNGKey(1905), half)
    opt_state = opt.init(params)
    params = jax.tree_map(lambda x: x[None], params)
    opt_state = jax.tree_map(lambda x: x[None], opt_state)
    # Do one successful param update
    params, opt_state = fn_update(params, opt_state, half)
    self.assertTrue(bool(opt_state.last_finite))
    # Check 2 rejected param updates
    for step in range(2):
      params, opt_state = fn_update(params, opt_state, two)
      self.assertFalse(bool(opt_state.last_finite))
      self.assertEqual(step + 1, int(opt_state.notfinite_count))
    # Next successful param update
    params, opt_state = fn_update(params, opt_state, half)
    self.assertTrue(bool(opt_state.last_finite))
    # Again 2 rejected param updates
    for step in range(2):
      params, opt_state = fn_update(params, opt_state, two)
      self.assertFalse(bool(opt_state.last_finite))
      self.assertEqual(step + 1, int(opt_state.notfinite_count))
    # Next param update with NaN is accepted since we reached maximum
    params, opt_state = fn_update(params, opt_state, two)
    self.assertEqual(5, int(opt_state.total_notfinite))

  @chex.variants(with_jit=True, without_jit=True, with_pmap=True)
  def test_multi_steps(self):
    batch_size = 32
    x_size = 7
    # Parameters should be updated only every `k_steps` optimisation steps.
    k_steps = 4
    data = jnp.ones([batch_size, x_size])

    def get_loss(x):
      loss = jnp.sum(hk.Linear(10)(x)**2)
      return loss

    loss_init, loss_apply = hk.without_apply_rng(hk.transform(get_loss))
    params = loss_init(jax.random.PRNGKey(1915), data)

    ms_opt = wrappers.MultiSteps(alias.adam(1e-4), k_steps)
    opt_init, opt_update = ms_opt.gradient_transformation()

    # Put the training in one function, to check that the update is indeed
    # jittable.
    def train_step(data, opt_state, params):
      grad = jax.grad(loss_apply)(params, data)
      updates, opt_state = opt_update(grad, opt_state, params)
      return updates, opt_state

    opt_state = opt_init(params)

    prev_loss = loss_apply(params, data)
    for idx in range(5 * k_steps):
      updates, opt_state = self.variant(train_step)(data, opt_state, params)
      new_params = update.apply_updates(params, updates)
      new_loss = loss_apply(new_params, data)
      if idx % k_steps < k_steps - 1:
        # The parameters should not have changed and the loss should be
        # constant.
        jax.tree_multimap(np.testing.assert_array_equal, new_params, params)
        np.testing.assert_equal(new_loss, prev_loss)
        self.assertFalse(ms_opt.has_updated(opt_state))
      else:
        # This is a step where parameters should actually have been updated, and
        # the loss should accordingly go down.
        np.testing.assert_array_less(new_loss, prev_loss)
        prev_loss = new_loss
        self.assertTrue(ms_opt.has_updated(opt_state))
      params = new_params

  def test_multi_steps_every_k_schedule(self):
    # Test a non-trivial schedule which varies over time.
    ms_opt = wrappers.MultiSteps(
        alias.sgd(1e-4), lambda grad_step: jnp.where(grad_step < 2, 1, 3))
    opt_init, opt_update = ms_opt.gradient_transformation()
    params = dict(a=jnp.zeros([]))
    opt_state = opt_init(params)
    grad = dict(a=jnp.zeros([]))
    self.assertFalse(ms_opt.has_updated(opt_state))
    # First two steps have 1 mini-step per update.
    for _ in range(2):
      _, opt_state = opt_update(grad, opt_state, params)
      self.assertTrue(ms_opt.has_updated(opt_state))
    # Subsequently, mini-steps should have 3 mini-steps per update.
    for _ in range(5):
      for _ in range(2):
        _, opt_state = opt_update(grad, opt_state, params)
        self.assertFalse(ms_opt.has_updated(opt_state))
      _, opt_state = opt_update(grad, opt_state, params)
      self.assertTrue(ms_opt.has_updated(opt_state))


class TestOptimizerState(transform.OptState):
  """Fast optimizer state for the lookahead tests."""
  aggregate_grads: transform.Params
  # Include a variable with non-zero initial value to check that it is reset
  # correctly by the lookahead optimizer.
  is_reset: bool = True


def test_optimizer(step_size: float) -> transform.GradientTransformation:
  """Fast optimizer for the lookahead tests."""

  # Use SGD for simplicity but add non-trivial optimizer state so that the
  # resetting behaviour of lookahead can be tested.
  def init_fn(params):
    aggregate_grads = jax.tree_map(jnp.zeros_like, params)
    return TestOptimizerState(aggregate_grads, is_reset=True)

  def update_fn(updates, state, params=None):
    del params  # unused by the test optimizer
    aggregate_grads = update.apply_updates(state.aggregate_grads, updates)
    updates = jax.tree_map(lambda u: step_size * u, updates)
    return updates, TestOptimizerState(aggregate_grads, is_reset=False)

  return transform.GradientTransformation(init_fn, update_fn)


class LookaheadTest(chex.TestCase):
  """Tests for the lookahead optimizer."""

  def setUp(self):
    super().setUp()
    self.grads = {'x': 2., 'y': -2}
    self.initial_params = {'x': 3., 'y': -3}
    self.synced_initial_params = wrappers.LookaheadParams.init_synced(
        self.initial_params)

  def loop(self, optimizer, num_steps, params):
    """Performs a given number of optimizer steps."""
    init_fn, update_fn = optimizer
    # Use the chex variant to check various function versions (jit, pmap, etc).
    step = self.variant(update_fn)
    opt_state = self.variant(init_fn)(params)
    for _ in range(num_steps):
      updates, opt_state = step(self.grads, opt_state, params)
      params = update.apply_updates(params, updates)

    return params, opt_state

  @chex.all_variants
  def test_lookahead(self):
    """Tests the lookahead optimizer in an analytically tractable setting."""
    sync_period = 3
    optimizer = wrappers.lookahead(
        test_optimizer(-0.5), sync_period=sync_period, slow_step_size=1 / 3)

    final_params, _ = self.loop(optimizer, 2 * sync_period,
                                self.synced_initial_params)
    # x steps must be: 3 -> 2 -> 1 -> 2 (sync) -> 1 -> 0 -> 1 (sync).
    # Similarly for y (with sign flipped).
    correct_final_params = {'x': 1, 'y': -1}
    chex.assert_tree_all_close(final_params.slow, correct_final_params)

  @chex.all_variants
  @parameterized.parameters([False], [True])
  def test_lookahead_state_reset(self, reset_state):
    """Checks that lookahead resets the fast optimizer state correctly."""
    num_steps = sync_period = 3
    fast_optimizer = test_optimizer(-0.5)
    optimizer = wrappers.lookahead(
        fast_optimizer,
        sync_period=sync_period,
        slow_step_size=0.5,
        reset_state=reset_state)

    _, opt_state = self.loop(optimizer, num_steps, self.synced_initial_params)
    fast_state = opt_state.fast_state
    if reset_state:
      correct_state = fast_optimizer.init(self.initial_params)
    else:
      _, correct_state = self.loop(fast_optimizer, num_steps,
                                   self.initial_params)

    chex.assert_tree_all_close(fast_state, correct_state)

  @chex.all_variants
  @parameterized.parameters(
      [1, 0.5, {'x': 1., 'y': -1.}],
      [1, 0, {'x': 3., 'y': -3.}],
      [1, 1, {'x': -1., 'y': 1.}],
      [2, 1, {'x': -1., 'y': 1.}])  # pyformat: disable
  def test_lookahead_edge_cases(self, sync_period, slow_step_size,
                                correct_result):
    """Checks special cases of the lookahed optimizer parameters."""
    # These edge cases are important to check since users might use them as
    # simple ways of disabling lookahead in experiments.
    optimizer = wrappers.lookahead(
        test_optimizer(-1), sync_period, slow_step_size)
    final_params, _ = self.loop(
        optimizer, num_steps=2, params=self.synced_initial_params)
    chex.assert_tree_all_close(final_params.slow, correct_result)


if __name__ == '__main__':
  absltest.main()
