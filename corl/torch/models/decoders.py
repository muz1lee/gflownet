# coding=utf-8
# Copyright (C) 2021. Huawei Technologies Co., Ltd. All rights reserved.
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


import torch
import torch.nn as nn

from ._base_network import PointerDecoder


class LSTMDecoder(PointerDecoder):
    """LSTM + Pointer Network"""

    def __init__(self, input_dim, hidden_dim, device=None) -> None:
        # input of Decoder is output of Encoder, e.g. embed_dim
        super(LSTMDecoder, self).__init__(input_dim=input_dim,
                                          hidden_dim=hidden_dim,
                                          device=device)
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.device = device
        self.lstm_cell = nn.LSTMCell(input_size=hidden_dim,
                                     hidden_size=hidden_dim,
                                     device=self.device)

    def forward(self, x) -> tuple:
        """"""
        self.batch_size = x.shape[0]
        self.seq_length = x.shape[1] # 10 
        self.encoder_output = x  

        s_i = torch.mean(x, 1)
        hi_ci = (torch.zeros((self.batch_size, self.hidden_dim), device=s_i.device),
                 torch.zeros((self.batch_size, self.hidden_dim), device=s_i.device))
        h_list = []
        c_list = []
        s_list = []
        action_list = []
        prob_list = []
        for step in range(self.seq_length): 
            h_list.append(hi_ci[0])
            c_list.append(hi_ci[1])
            s_list.append(s_i)
            print('s_i',s_i)
            
            
            # 一步步确定顺序
            s_i, hi_ci, pos, prob = self.step_decode(input=s_i, state=hi_ci)
            # next_input, state, action, masked_scores
            action_list.append(pos)
            prob_list.append(prob)

        h_list = torch.stack(h_list, dim=1).squeeze()  # [Batch,seq_length,hidden]
        c_list = torch.stack(c_list, dim=1).squeeze()  # [Batch,seq_length,hidden]
        s_list = torch.stack(s_list, dim=1).squeeze()  # [Batch,seq_length,hidden]
        # print('s_list',s_list)
        # Stack visited indices
        actions = torch.stack(action_list, dim=1)  # [Batch,seq_length]
        mask_scores = torch.stack(prob_list, dim=1)  # [Batch,seq_length,seq_length]
        self.mask = torch.zeros(1, device=self.device)

        return actions, mask_scores, s_list, h_list, c_list


    
# Add new variables to calculate inflow and outflow 

class MLPDecoder(PointerDecoder):
    """Multi Layer Perceptions + Pointer Network"""

    def __init__(self, input_dim, hidden_dim, device=None) -> None:
        super(MLPDecoder, self).__init__(input_dim=input_dim,
                                          hidden_dim=hidden_dim,
                                         device=device)
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.device = device
        self.mlp = self.feedforward_mlp

    def forward(self, x) -> tuple:

        self.batch_size = x.shape[0]
        self.seq_length = x.shape[1]
        self.encoder_output = x

        s_i = torch.mean(x, 1) # s_0

        s_list = []
        action_list = []
        prob_list = []
        in_flow_list = []
        next_q_list=[]
        for step in range(self.seq_length):
            s_list.append(s_i)
            s_i, _, pos, prob,in_flow,next_q = self.step_decode(input=s_i, state=None)
            # next_input, _ , action, masked_scores 
            # 输入input，得到一个新的state: s_i --> next_input

            # prob就是masked_score,就是gflownet里面self.models(s)
            action_list.append(pos)
            prob_list.append(prob)
            in_flow_list.append(in_flow)
            next_q_list.append(next_q)
        s_list = torch.stack(s_list, dim=1).squeeze()  # [Batch,seq_length,hidden]

        # Stack visited indices
        actions = torch.stack(action_list, dim=1)  # [Batch,seq_length]
        mask_scores = torch.stack(prob_list, dim=1)  # [Batch,seq_length,seq_length]
        self.mask = torch.zeros(1, device=self.device)
        in_flow_list = torch.stack(in_flow_list, dim=1) # [Batch,seq_length]
        next_q_list = torch.stack(next_q_list, dim=1) # [Batch,seq_length,seq_length]
        
        return actions,action_list,prob_list, mask_scores, s_list,in_flow_list,next_q_list
