import argparse
import copy
import gzip
import heapq
import itertools
import os
import pickle
from collections import defaultdict
from itertools import count

import numpy as np
from scipy.stats import norm
from tqdm import tqdm
import torch
import torch.nn as nn
from torch.distributions.categorical import Categorical



parser = argparse.ArgumentParser()

parser.add_argument("--save_path", default='results/flow_insp_0.pkl.gz', type=str)
parser.add_argument("--init_data_path", default='results/e2e', type=str)
parser.add_argument("--learning_rate", default=2.5e-5, help="Learning rate", type=float)
parser.add_argument("--method", default='flownet', type=str)
parser.add_argument("--opt", default='adam', type=str)
parser.add_argument("--adam_beta1", default=0.9, type=float)
parser.add_argument("--adam_beta2", default=0.999, type=float)
parser.add_argument("--momentum", default=0.9, type=float)
parser.add_argument("--bootstrap_tau", default=0.1, type=float)
parser.add_argument("--kappa", default=0, type=float)
parser.add_argument("--mbsize", default=16, help="Minibatch size", type=int)
parser.add_argument("--bufsize", default=16, help="MCMC buffer size", type=int)
parser.add_argument("--train_to_sample_ratio", default=1, type=float)
parser.add_argument("--horizon", default=4, type=int)
parser.add_argument("--ndim", default=2, type=int)
parser.add_argument("--n_hid", default=256, type=int)
parser.add_argument("--n_layers", default=2, type=int)
parser.add_argument("--n_train_steps", default=100, type=int)
parser.add_argument("--num_empirical_loss", default=200000, type=int,
                    help="Number of samples used to compute the empirical distribution loss")
parser.add_argument('--func', default='corners_floor_B')
parser.add_argument("--num_val_iters", default=500, type=int)
parser.add_argument("--reward_topk", default=5, type=int)
parser.add_argument("--reward_lambda", default=0, type=float)
parser.add_argument("--inf_batch_size", default=32, type=int)
parser.add_argument("--num_samples", default=8, type=int)
parser.add_argument("--num_init_points", default=10, type=int)
parser.add_argument("--num_val_points", default=128, type=int)
parser.add_argument("--num_iter", default=10, type=int)
parser.add_argument("--use_model", action='store_true')

parser.add_argument("--replay_strategy", default='none', type=str) # top_k none
parser.add_argument("--replay_sample_size", default=2, type=int)
parser.add_argument("--replay_buf_size", default=100, type=float)

parser.add_argument("--ppo_num_epochs", default=32, type=int) # number of SGD steps per epoch
parser.add_argument("--ppo_epoch_size", default=16, type=int) # number of sampled minibatches per epoch
parser.add_argument("--ppo_clip", default=0.2, type=float)
parser.add_argument("--ppo_entropy_coef", default=4e-1, type=float)
parser.add_argument("--clip_grad_norm", default=0., type=float)


# This is alpha in the note, smooths the learned distribution into a uniform exploratory one
parser.add_argument("--device", default='cpu', type=str)
parser.add_argument("--progress", action='store_true')

_dev = [torch.device('cpu')]
tf = lambda x: torch.FloatTensor(x).to(_dev[0])
tl = lambda x: torch.LongTensor(x).to(_dev[0])


def set_device(dev):
    _dev[0] = dev

# Reward function
def func_corners(x):
    ax = abs(x)
    return (ax > 0.5).prod(-1) * 0.5 + ((ax < 0.8) * (ax > 0.6)).prod(-1) * 2 + 1e-1

def func_corners_floor_B(x):
    ax = abs(x)
    return (ax > 0.5).prod(-1) * 0.5 + ((ax < 0.8) * (ax > 0.6)).prod(-1) * 2 + 1e-2

def func_corners_floor_A(x):
    ax = abs(x)
    return (ax > 0.5).prod(-1) * 0.5 + ((ax < 0.8) * (ax > 0.6)).prod(-1) * 2 + 1e-3

def func_cos_N(x):
    ax = abs(x)
    return ((np.cos(x * 50) + 1) * norm.pdf(x * 5)).prod(-1) + 0.01

class GridEnv:

    def __init__(self, horizon, ndim=2, xrange=[-1, 1], func=None, allow_backward=False):
        self.horizon = horizon
        self.start = [xrange[0]] * ndim
        self.ndim = ndim
        self.width = xrange[1] - xrange[0]
        self.func = (
            (lambda x: ((np.cos(x * 50) + 1) * norm.pdf(x * 5)).prod(-1) + 0.01)
            if func is None else func)
        self.xspace = np.linspace(*xrange, horizon) # 生成-1到1的等差数列(8) , *parameter 用来接受任意多个参数并将其放在一个元组中
        self.allow_backward = allow_backward  # If true then this is a MCMC ergodic env, otherwise a DAG
        self._true_density = None

    def obs(self, s=None):
        s = np.int32(self._state if s is None else s)
        z = np.zeros((self.horizon * self.ndim), dtype=np.float32)
        z[np.arange(len(s)) * self.horizon + s] = 1
        return z

    def s2x(self, s):
        return (self.obs(s).reshape((self.ndim, self.horizon)) * self.xspace[None, :]).sum(1)

    def reset(self):
        self._state = np.int32([0] * self.ndim)
        self._step = 0
        return self.obs(), self.func(self.s2x(self._state)), self._state

    def parent_transitions(self, s, used_stop_action):
        if used_stop_action:
            return [self.obs(s)], [self.ndim]
        parents = []
        actions = []
        for i in range(self.ndim):
            if s[i] > 0:
                sp = s + 0
                sp[i] -= 1
                if sp.max() == self.horizon-1: # can't have a terminal parent
                    continue
                parents += [self.obs(sp)]
                actions += [i]
        return parents, actions

    def step(self, a, s=None):
        if self.allow_backward: # MCMC
            return self.step_chain(a, s)
        return self.step_dag(a, s)

    def step_dag(self, a, s=None):
        '''
        input: action

        output:
        observation_ ,
        reward
        done (True / False)
        s_

        '''
        _s = s

        s = (self._state if s is None else s) + 0
        print('------------------------------------------')
        print('actions:',a)
        print('parent_s:',s)
        print('observation(parent_s)', self.obs(s))
        if a < self.ndim:
            s[a] += 1


        done = s.max() >= self.horizon - 1 or a == self.ndim

        if _s is None:
            self._state = s
            self._step += 1
        print('state',s)
        print('observation(s)',self.obs(s))
        print('reward', self.func(self.s2x(s)))
        print('done',done)
        return self.obs(s), 0 if not done else self.func(self.s2x(s)), done, s

    def step_chain(self, a, s=None):
        _s = s
        s = (self._state if s is None else s) + 0
        sc = s + 0
        if a < self.ndim:
            s[a] = min(s[a]+1, self.horizon-1)
        if a >= self.ndim:
            s[a-self.ndim] = max(s[a-self.ndim]-1,0)

        reverse_a = ((a + self.ndim) % (2 * self.ndim)) if any(sc != s) else a

        if _s is None:
            self._state = s
            self._step += 1
        return self.obs(s), self.func(self.s2x(s)), s, reverse_a

    def true_density(self):
        """
        Computes the reward density (reward / sum(rewards)) of the whole space , if the dimensionality is smaller than
        the specified arguments .

        Returns
        ----------
        Tuple:
          - normalized reward for each state
          - states
          - (un-normalized) reward)
        """
        if self._true_density is not None:
            return self._true_density
        all_int_states = np.int32(list(itertools.product(*[list(range(self.horizon))]*self.ndim)))
        state_mask = np.array([len(self.parent_transitions(s, False)[0]) > 0 or sum(s) == 0
                               for s in all_int_states])
        all_xs = (np.float32(all_int_states) / (self.horizon-1) *
                  (self.xspace[-1] - self.xspace[0]) + self.xspace[0])
        traj_rewards = self.func(all_xs)[state_mask]
        self._true_density = (traj_rewards / traj_rewards.sum(),
                              list(map(tuple,all_int_states[state_mask])),
                              traj_rewards)
        return self._true_density

    def all_possible_states(self):
        """Compute quantities for debugging and analysis"""
        # all possible action sequences
        def step_fast(a, s):
            s = s + 0
            s[a] += 1
            return s
        f = lambda a, s: (
            [np.int32(a)] if np.max(s) == self.horizon - 1 else
            [np.int32(a+[self.ndim])]+sum([f(a+[i], step_fast(i, s)) for i in range(self.ndim)], []))
        all_act_seqs = f([], np.zeros(self.ndim, dtype='int32'))
        # all RL states / intermediary nodes
        all_int_states = list(itertools.product(*[list(range(self.horizon))]*self.ndim))
        # Now we need to know for each partial action sequence what
        # the corresponding states are. Here we can just count how
        # many times we moved in each dimension:
        all_traj_states = np.int32([np.bincount(i[:j], minlength=self.ndim+1)[:-1]
                                   for i in all_act_seqs
                                   for j in range(len(i))])
        # all_int_states is ordered, so we can map a trajectory to its
        # index via a sum
        arr_mult = np.int32([self.horizon**(self.ndim-i-1)
                             for i in range(self.ndim)])
        all_traj_states_idx = (
            all_traj_states * arr_mult[None, :]
        ).sum(1)
        # For each partial trajectory, we want the index of which trajectory it belongs to
        all_traj_idxs = [[j]*len(i) for j,i in enumerate(all_act_seqs)]
        # For each partial trajectory, we want the index of which state it leads to
        all_traj_s_idxs = [(np.bincount(i, minlength=self.ndim+1)[:-1] * arr_mult).sum()
                           for i in all_act_seqs]
        # Vectorized
        a = torch.cat(list(map(torch.LongTensor, all_act_seqs)))
        u = torch.LongTensor(all_traj_states_idx)
        v1 = torch.cat(list(map(torch.LongTensor, all_traj_idxs)))
        v2 = torch.LongTensor(all_traj_s_idxs)
        # With all this we can do an index_add, given
        # pi(all_int_states):
        def compute_all_probs(policy_for_all_states):
            """computes p(x) given pi(a|s) for all s"""
            dev = policy_for_all_states.device
            pi_a_s = torch.log(policy_for_all_states[u, a])
            q = torch.exp(torch.zeros(len(all_act_seqs), device=dev)
                                      .index_add_(0, v1, pi_a_s))
            q_sum = (torch.zeros((all_xs.shape[0],), device=dev)
                     .index_add_(0, v2, q))
            return q_sum[state_mask]
        # some states aren't actually reachable
        state_mask = np.bincount(all_traj_s_idxs, minlength=len(all_int_states)) > 0
        # Let's compute the reward as well
        all_xs = (np.float32(all_int_states) / (self.horizon-1) *
                  (self.xspace[-1] - self.xspace[0]) + self.xspace[0])
        traj_rewards = self.func(all_xs)[state_mask]
        # All the states as the agent sees them:
        all_int_obs = np.float32([self.obs(i) for i in all_int_states])
        print(all_int_obs.shape, a.shape, u.shape, v1.shape, v2.shape)
        return all_int_obs, traj_rewards, all_xs, compute_all_probs

def make_mlp(l, act=nn.LeakyReLU(), tail=[]):
    """makes an MLP with no top layer activation"""
    return nn.Sequential(* (sum(
        [[nn.Linear(i, o)] + ([act] if n < len(l)-2 else [])
         for n, (i, o) in enumerate(zip(l, l[1:]))], []) + tail)  )


class ReplayBuffer:
    """ RL algorithms use reply buffers to store trajectories of experience when executing a policy in an environment """
    def __init__(self, args, env):
        self.buf = []
        self.strat = args.replay_strategy # None
        self.sample_size = args.replay_sample_size # 2
        self.bufsize = args.replay_buf_size # 100
        self.env = env

    def add(self, x, r_x):
        if self.strat == 'top_k':
            if len(self.buf) < self.bufsize or r_x > self.buf[0][0]:
                self.buf = sorted(self.buf + [(r_x, x)])[-self.bufsize:]

    def sample(self):
        print('call sample function in replaybuffer ')
        if not len(self.buf):
            return []
        idxs = np.random.randint(0, len(self.buf), self.sample_size)
        return sum([self.generate_backward(*self.buf[i]) for i in idxs], [])

    def generate_backward(self, r, s0):
        s = np.int8(s0)
        os0 = self.env.obs(s)
        # If s0 is a forced-terminal state, the the action that leads
        # to it is s0.argmax() which .parents finds, but if it isn't,
        # we must indicate that the agent ended the trajectory with
        # the stop action
        used_stop_action = s.max() < self.env.horizon - 1
        done = True

        # Now we work backward from that last transition
        traj = []
        while s.sum() > 0:
            parents, actions = self.env.parent_transitions(s, used_stop_action)
            # add the transition
            traj.append([tf(i) for i in (parents, actions, [r], [self.env.obs(s)], [done])])
            # Then randomly choose a parent state
            if not used_stop_action:
                i = np.random.randint(0, len(parents))
                a = actions[i]
                s[a] -= 1
            # Values for intermediary trajectory states:
            used_stop_action = False
            done = False
            r = 0
        return traj

class FlowNetAgent:
    def __init__(self, args, envs):
        self.model = make_mlp([args.horizon * args.ndim] +
                              [args.n_hid] * args.n_layers +
                              [args.ndim+1])
        #[16, 256, 256, 3]
        """
        Sequential(
          (0): Linear(in_features=16, out_features=256, bias=True)
          (1): LeakyReLU(negative_slope=0.01)
          (2): Linear(in_features=256, out_features=256, bias=True)
          (3): LeakyReLU(negative_slope=0.01)
          (4): Linear(in_features=256, out_features=3, bias=True)
        )
        """

        self.model.to(args.dev)
        self.target = copy.deepcopy(self.model)
        self.envs = envs
        self.ndim = args.ndim
        self.tau = args.bootstrap_tau
        self.replay = ReplayBuffer(args, envs[0])

    def parameters(self):
        return self.model.parameters()

    def sample_many(self, mbsize, all_visited):

        batch = []
        batch += self.replay.sample()
        s = tf([i.reset()[0] for i in self.envs])

        done = [False] * mbsize
        while not all(done):

            # Note to self: this is ugly, ugly code
            with torch.no_grad():

                acts = Categorical(logits=self.model(s)).sample()

            # step : list
            # - For each e in envs, if corresponding done is False
            #   - For each element i in env, and a in acts
            #     - i.step(a)  [ call step_dag() function ]

            step = [i.step(a) for i,a in zip( [e for d, e in zip(done, self.envs) if not d], acts)]

            #print('step',step)
            # step.dag - return self.obs(s), 0 if not done else self.func(self.s2x(s)), done, s

            # if a == self.ndim , used_stop_action = True
            p_a = [self.envs[0].parent_transitions(sp_state, a == self.ndim)
                   for a, (sp, r, done, sp_state) in zip(acts, step)]
            print('parent_state',p_a[0])
            print('_________')
            # return parents, actions

            batch += [[tf(i) for i in (p, a, [r], [sp], [d])]
                      for (p, a), (sp, r, d, _) in zip(p_a, step)]

            c = count(0)

            m = {j:next(c) for j in range(mbsize) if not done[j]} # count the number of envs which have not yet done

            done = [bool(d or step[  m[i] ][2]) for i, d in enumerate(done)] # step[][2] -> True / False

            s = tf([i[0] for i in step if not i[2]]) # if have not done , save step[][0] -> self.obs(s)

            for (_, r, d, sp) in step:
                if d: # if done
                    all_visited.append(tuple(sp))
                    self.replay.add(tuple(sp), r)
        return batch


    def learn_from(self, it, batch):

        """
        Computes the loss of a batch

        Args
        ----
        it : int
            Iteration
        batch : ndarray
            A batch of data: every row is a state (list), corresponding to all states visited in each sequence in the batch.
        Returns
        -------
        loss : float
            Loss, as per Equation 12 of https://arxiv.org/abs/2106.04399v1
        term_loss : float
            Loss of the terminal nodes only
        flow_loss : float
            Loss of the intermediate nodes only


        """

        loginf = tf([1000])
        batch_idxs = tl(sum([[i]*len(parents) for i, (parents,_,_,_,_) in enumerate(batch)], []))
        parents, actions, r, sp, done = map(torch.cat, zip(*batch))
        parents_Qsa = self.model(parents)[torch.arange(parents.shape[0]), actions.long()]
        in_flow = torch.log(torch.zeros((sp.shape[0],))
                            .index_add_(0, batch_idxs, torch.exp(parents_Qsa)))
        if self.tau > 0:
            with torch.no_grad():
                next_q = self.target(sp)
        else:
            next_q = self.model(sp)

        next_qd = next_q * (1-done).unsqueeze(1) + done.unsqueeze(1) * (-loginf)
        out_flow = torch.logsumexp(torch.cat([torch.log(r)[:, None], next_qd], 1), 1)
        loss = (in_flow - out_flow).pow(2).mean()

        with torch.no_grad():
            term_loss = ((in_flow - out_flow) * done).pow(2).sum() / (done.sum() + 1e-20)
            flow_loss = ((in_flow - out_flow) * (1-done)).pow(2).sum() / ((1-done).sum() + 1e-20)

        if self.tau > 0:
            for a,b in zip(self.model.parameters(), self.target.parameters()):
                b.data.mul_(1-self.tau).add_(self.tau*a)

        return loss, term_loss, flow_loss


def make_opt(params, args):
    params = list(params)
    if not len(params):
        return None
    if args.opt == 'adam':
        opt = torch.optim.Adam(params, args.learning_rate,
                               betas=(args.adam_beta1, args.adam_beta2))
    elif args.opt == 'msgd':
        opt = torch.optim.SGD(params, args.learning_rate, momentum=args.momentum)
    return opt


def compute_empirical_distribution_error(env, visited):
    if not len(visited):
        return 1, 100
    hist = defaultdict(int)
    for i in visited:
        hist[i] += 1
    td, end_states, true_r = env.true_density()
    true_density = tf(td)
    Z = sum([hist[i] for i in end_states])
    estimated_density = tf([hist[i] / Z for i in end_states])
    k1 = abs(estimated_density - true_density).mean().item()
    # KL divergence
    kl = (true_density * torch.log(estimated_density / true_density)).sum().item()
    return k1, kl

def main(args):

    args.dev = torch.device(args.device)
    set_device(args.dev)
    f = {'default': None,
         'cos_N': func_cos_N,
         'corners': func_corners,
         'corners_floor_A': func_corners_floor_A,
         'corners_floor_B': func_corners_floor_B,
    }[args.func]

    args.is_mcmc = args.method in ['mars', 'mcmc']

    env = GridEnv(args.horizon, args.ndim, func=f, allow_backward=args.is_mcmc)
    envs = [GridEnv(args.horizon, args.ndim, func=f, allow_backward=args.is_mcmc)
            for i in range(args.bufsize)]
    ndim = args.ndim
    if args.method == 'flownet':
        agent = FlowNetAgent(args, envs)

    opt = make_opt(agent.parameters(), args)

    # metrics
    all_losses = []
    all_visited = []
    empirical_distrib_losses = []

    ttsr = max(int(args.train_to_sample_ratio), 1) #  train ratop to sample :  1
    sttr = max(int(1/args.train_to_sample_ratio), 1) # sample to train ratio : 1


    for i in tqdm(range(args.n_train_steps+1), disable=not args.progress):
        data = []
        for j in range(sttr):
            data += agent.sample_many(args.mbsize, all_visited) # mbsize = 16
        for j in range(ttsr):
            losses = agent.learn_from(i * ttsr + j, data) # returns (opt loss, *metrics)
            if losses is not None:
                losses[0].backward()   # 反向传播计算梯度

                opt.step() # 更新所有参数
                opt.zero_grad()   # 将模型的参数梯度初始化为0
                all_losses.append([i.item() for i in losses])


        if not i % 100:
            empirical_distrib_losses.append(
                compute_empirical_distribution_error(env, all_visited[-args.num_empirical_loss:]))
            if args.progress:
                k1, kl = empirical_distrib_losses[-1]
                print('empirical L1 distance', k1, 'KL', kl)
                if len(all_losses):
                    print(*[f'{np.mean([i[j] for i in all_losses[-100:]]):.5f}'
                            for j in range(len(all_losses[0]))])

    root = os.path.split(args.save_path)[0]
    os.makedirs(root, exist_ok=True)
    pickle.dump(
        {'losses': np.float32(all_losses),
         #'model': agent.model.to('cpu') if agent.model else None,
         'params': [i.data.to('cpu').numpy() for i in agent.parameters()],
         'visited': np.int8(all_visited),
         'emp_dist_loss': empirical_distrib_losses,
         'true_d': env.true_density()[0],
         'args':args},
        gzip.open(args.save_path, 'wb'))

if __name__ == '__main__':
    args = parser.parse_args()
    torch.set_num_threads(1)
    main(args)

