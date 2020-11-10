# torch_layers.py

import os, sys, pickle, time, shutil, logging, copy
import math, numpy, scipy
numpy.random.seed(545)
import torch
torch.manual_seed(545)

from modules import make_logger

'''
Pytorch Based Layers
'''

########################
# PyTorch-based Layers #
########################

def batch_norm_D_tensor(input_tensor, bn_fn, D_index):
    # Move D_index to 1, to norm D
    h_SDB = torch.transpose(input_tensor, 1, D_index)
    h_SDB = bn_fn(h_SDB)
    # Reshape back, swap 1 and D_index again
    h_SBD = torch.transpose(h_SDB, 1, D_index)
    if D_index > 2:
        h_SBD = h_SBD.contiguous()
    return h_SBD

def compute_f_nlf(x_dict):
    log_f_mean = 5.04418
    log_f_std  = 0.358402
    if 'nlf_SBM' in x_dict:
        nlf = x_dict['nlf_SBM']
        lf = torch.add(torch.mul(nlf, log_f_std), log_f_mean) # S*B*M
        f  = torch.exp(lf)                                              # S*B*M
    elif 'f_SBM' in x_dict:
        f = x_dict['f_SBM']
        lf = torch.log(f)
        nlf = torch.mul(torch.add(lf, (-1)*log_f_mean), 1./log_f_std)
    return f, nlf


class Build_DV_Y_Input_Layer(object):
    ''' This layer has only parameters, no torch.nn.module '''
    ''' Mainly for the prev_layer argument '''
    def __init__(self, dv_y_cfg):
        self.dv_y_cfg = dv_y_cfg
        if dv_y_cfg.y_feat_name == 'wav':
            self.init_wav(dv_y_cfg)
        elif dv_y_cfg.y_feat_name == 'cmp':
            self.init_cmp(dv_y_cfg)

    def init_wav(self, dv_y_cfg):
        self.params = {}
        self.params["output_dim_seq"]      = ['S', 'B', 'M', 'T']
        self.params["output_dim_values"]   = {'S':dv_y_cfg.input_data_dim['S'], 'B':dv_y_cfg.input_data_dim['B'], 'M':dv_y_cfg.input_data_dim['M'], 'T':dv_y_cfg.input_data_dim['T_S']}

    def init_cmp(self, dv_y_cfg):
        self.params = {}
        self.params["output_dim_seq"]      = ['S', 'B', 'D']
        self.params["output_dim_values"]   = {'S':dv_y_cfg.input_data_dim['S'], 'B':dv_y_cfg.input_data_dim['B'], 'D':dv_y_cfg.input_data_dim['D']}

class Build_FC_Layer(torch.nn.Module):
    """
    Fully-connected layer
    Operation to the last dimension
    Output last dimension is 'D'
    1. Linear transform
    2. Batch Norm, if needed
    3. Activation function e.g. ReLU, LReLU; None for linear layer
    """
    def __init__(self, params):
        super().__init__()
        self.params = params
        layer_config = self.params["layer_config"]

        D_name = self.params["input_dim_seq"][-1]
        assert D_name == 'D'
        D_in   = self.params["input_dim_values"][D_name]
        D_out  = layer_config['size']

        self.params["output_dim_seq"] = self.params["input_dim_seq"]
        self.params["output_dim_seq"][-1] = 'D'
        self.params["output_dim_values"] = copy.deepcopy(self.params["input_dim_values"])
        self.params["output_dim_values"]['D'] = D_out

        self.linear_fn = torch.nn.Linear(D_in, D_out)

        self.batch_norm = layer_config["batch_norm"]
        if self.batch_norm:
            self.D_index = len(self.params["input_dim_seq"]) - 1
            self.bn_fn = torch.nn.BatchNorm1d(D_out)
        self.activation_fn = self.params['activation_fn']

    def forward(self, x_dict):
        if 'h' in x_dict:
            x = x_dict['h']
        elif 'x' in x_dict:
            x = x_dict['x']
        # Linear
        h_i = self.linear_fn(x)
        # Batch Norm
        if self.batch_norm:
            h_i = batch_norm_D_tensor(h_i, self.bn_fn, D_index=self.D_index) # Batch Norm on last index
        # Activation
        if self.activation_fn is None:
            h = h_i
        else:
            h = self.activation_fn(h_i)
        y_dict = {'h': h}
        return y_dict
        
class Build_Tensor_Reshape(torch.nn.Module):
    """
    Very specific reshape methods
    Each method requires 1 definition, 1 forward function
    """
    def __init__(self, params):
        super().__init__()
        self.params = params

        construct_layer = getattr(self, self.params["layer_config"]["io_name"])
        construct_layer()

    def forward(self, x_dict):
        ''' 
        To be defined in each function
        '''
        pass

    def compute_num_seq(self, T_total, T_win, shift_win):
        return int((T_total - T_win) / shift_win) + 1

    def convert_f_2_nlf(self, f):
        log_f_mean = 5.04418
        log_f_std  = 0.358402
        lf  = torch.log(f)
        nlf = torch.mul(torch.add(lf, (-1)*log_f_mean), 1./log_f_std)
        return nlf

    def wav_ST_2_wav_SBMT(self):
        '''
        Compute SBMT from ST
        '''
        win_len_shift_list = self.params["layer_config"]["win_len_shift_list"]
        S = self.params["input_dim_values"]['S']
        T = self.params["input_dim_values"]['T']
        B = self.compute_num_seq(T, win_len_shift_list[0][0], win_len_shift_list[0][1])
        M = self.compute_num_seq(win_len_shift_list[0][0], win_len_shift_list[1][0], win_len_shift_list[1][1])

        self.params["output_dim_seq"] = ['S', 'B', 'M', 'T']
        self.params['output_dim_values'] = {'S': S, 'B': B, 'M': M, 'T': win_len_shift_list[1][0]}

        self.forward = self.wav_ST_2_wav_SBMT_fn

    def wav_ST_2_wav_SBMT_fn(self, x_dict):
        '''
        Unfold wav twice; S*T --> S*B*M*T
        '''
        wav = x_dict['wav_ST']
        win_len_shift_list = self.params["layer_config"]["win_len_shift_list"]
        for i in range(2):
            win_len, win_shift = win_len_shift_list[i]
            wav = wav.unfold(i+1, win_len, win_shift)
        y_dict = self.copy_dict(x_dict, except_List=['wav_ST'])
        y_dict['wav_SBMT'] = wav
        return y_dict

    def concat_wav_nlf_tau_vuv(self):
        '''
        Concatenate wav_SBMT, nlf_SBM, tau_SBM, vuv_SBM
        Output: h_SBMD; D <-- T+3
        Convert f to nlf
        '''
        S = self.params["input_dim_values"]['S']
        B = self.params["input_dim_values"]['B']
        M = self.params["input_dim_values"]['M']
        T = self.params["input_dim_values"]['T']

        self.params["output_dim_seq"] = ['S', 'B', 'M', 'D']
        self.params['output_dim_values'] = {'S': S, 'B': B, 'M': M, 'D': T+3}

        self.forward = self.concat_wav_nlf_tau_vuv_fn

    def concat_wav_nlf_tau_vuv_fn(self, x_dict):
        '''
        Concatenate wav_SBMT, nlf_SBM, tau_SBM
        Output: h_SBMD; D <-- T+3
        Convert f to nlf
        '''
        wav = x_dict['wav_SBMT']
        if 'nlf_SBM' in x_dict:
            nlf = x_dict['nlf_SBM']
        else:
            nlf = self.convert_f_2_nlf(x_dict['f_SBM'])
        tau = x_dict['tau_SBM']
        vuv = x_dict['vuv_SBM']

        nlf_1 = torch.unsqueeze(nlf, 3) # S*B*M --> # S*B*M*1
        tau_1 = torch.unsqueeze(tau, 3) # S*B*M --> # S*B*M*1
        vuv_1 = torch.unsqueeze(vuv, 3) # S*B*M --> # S*B*M*1

        h = torch.cat([wav, nlf_1, tau_1, vuv_1], 3)
        y_dict = {'h': h}
        return y_dict

    def h_SBMD_2_h_SBD(self):
        '''
        Reshape; D <-- M*D
        '''
        S = self.params["input_dim_values"]['S']
        B = self.params["input_dim_values"]['B']
        M = self.params["input_dim_values"]['M']
        D = self.params["input_dim_values"]['D']

        self.params["output_dim_seq"] = ['S', 'B', 'D']
        self.params['output_dim_values'] = {'S': S, 'B': B, 'D': M*D}

        self.forward = self.h_SBMD_2_h_SBD_fn

    def h_SBMD_2_h_SBD_fn(self, x_dict):
        '''
        h_SBMD --> h_SBD
        '''
        h_SBMD = x_dict['h']
        h_size = h_SBMD.size()
        h_SBD  = h_SBMD.view([h_size[0], h_size[1], -1])
        y_dict = {'h': h_SBD}
        return y_dict

    def copy_dict(self, x_dict, except_List=[]):
        '''
        Copy every key-value pair to the new dict, except keys in the list
        '''
        y_dict = {}
        for k in x_dict:
            if k in except_List:
                pass
            else:
                y_dict[k] = x_dict[k]
        return y_dict

    

class Build_NN_Layer(torch.nn.Module):
    def __init__(self, layer_config, prev_layer=None):
        super().__init__()
        self.params = {}
        self.params["layer_config"] = layer_config
        self.params["type"] = layer_config['type']

        # Extract dimension information from previous layer, or specify in params
        if prev_layer is not None:
            self.params["input_dim_seq"]    = prev_layer.params["output_dim_seq"]
            self.params["input_dim_values"] = prev_layer.params["output_dim_values"]

        construct_layer = getattr(self, self.params["layer_config"]["type"])
        construct_layer()
        self.params   = self.layer_fn.params

        ''' Dropout '''
        try: 
            self.params["dropout_p"] = self.params["layer_config"]['dropout_p']
        except KeyError: 
            self.params["dropout_p"] = 0.
        if self.params["dropout_p"] > 0:
            self.dropout_fn = torch.nn.Dropout(p=self.params["dropout_p"])

    def forward(self, x_dict):
        y_dict = self.layer_fn(x_dict)
        if self.params["dropout_p"] > 0:
            y_dict['h'] = self.dropout_fn(y_dict['h'])
        return y_dict

    def Linear(self, activation_fn=None):
        self.params['activation_fn'] = None
        self.layer_fn = Build_FC_Layer(self.params)

    def ReLU(self):
        self.params['activation_fn'] = torch.nn.ReLU()
        self.layer_fn = Build_FC_Layer(self.params)

    def LReLU(self):
        self.params['activation_fn'] = torch.nn.LeakyReLU()
        self.layer_fn = Build_FC_Layer(self.params)

    def Tensor_Reshape(self):
        self.layer_fn = Build_Tensor_Reshape(self.params)

    def DW3(self, activation_fn=torch.nn.LeakyReLU()):
        '''
        DNN: wav and 3 features
        '''
        self.params['activation_fn'] = activation_fn
        self.layer_fn = Build_DNN_wav_3_nlf_tau_vuv(self.params)

    def Sinenet_V1(self, activation_fn=torch.nn.LeakyReLU()):
        self.params['activation_fn'] = activation_fn
        self.layer_fn = Build_Sinenet_V1(self.params)

    def Sinenet_V2(self, activation_fn=torch.nn.LeakyReLU()):
        self.params['activation_fn'] = activation_fn
        self.layer_fn = Build_Sinenet_V2(self.params)

    def Sinenet_V1_Residual(self, activation_fn=torch.nn.LeakyReLU()):
        self.params['activation_fn'] = activation_fn
        self.layer_fn = Build_Sinenet_V1_Residual(self.params)

class Build_DNN_wav_3_nlf_tau_vuv(torch.nn.Module):
    ''' 
        Inputs: wav_SBMT, f0_SBM, tau_SBM, vuv_SBM
        Output: h: S*B*M*D
        1. Use 2 separate Linear_fn for x, and nlf_SBM + tau_SBM + vuv_SBM
        2. Add the 2; batch_norm, activation_fn
    '''
    def __init__(self, params):
        super().__init__()
        self.params = params
        layer_config = self.params["layer_config"]

        self.params["output_dim_seq"] = ['S', 'B', 'M', 'D']
        self.params["output_dim_values"] = {'S': self.params["input_dim_values"]['S'], 'B': self.params["input_dim_values"]['B'], 'M': self.params["input_dim_values"]['M'], 'D': layer_config['size']}

        self.linear_fn_1 = torch.nn.Linear(self.params["input_dim_values"]['T'], layer_config['size'])
        self.linear_fn_2 = torch.nn.Linear(3, layer_config['size'])
        self.batch_norm = layer_config["batch_norm"]
        if self.batch_norm:
            self.D_index = len(self.params["input_dim_seq"]) - 1
            self.bn_fn = torch.nn.BatchNorm2d(layer_config['size'])
        self.activation_fn = self.params['activation_fn']

    def forward(self, x_dict):
        
        if 'wav_SBMT' in x_dict:
            x = x_dict['wav_SBMT']
        elif 'h' in x_dict:
            x = x_dict['h']
        
        f, nlf = compute_f_nlf(x_dict)
        tau = x_dict['tau_SBM']
        vuv = x_dict['vuv_SBM']
        
        y_SBMD_1  = self.linear_fn_1(x) # S*B*M*T -> S*B*M*D
        # nlf, tau, vuv
        nlf_1 = torch.unsqueeze(nlf, 3)
        tau_1 = torch.unsqueeze(tau, 3)
        vuv_1 = torch.unsqueeze(vuv, 3)
        nlf_tau_vuv = torch.cat([nlf_1, tau_1, vuv_1], 3)
        y_SBMD_2 = self.linear_fn_2(nlf_tau_vuv) # S*B*M*3 -> S*B*M*D

        y_SBMD = y_SBMD_1 + y_SBMD_2
        # Batch Norm
        if self.batch_norm:
            y_SBMD = batch_norm_D_tensor(y_SBMD, self.bn_fn, index_D=self.D_index)

        # ReLU
        h_SBMD = self.activation_fn(y_SBMD)

        y_dict = {'h': h_SBMD}
        return y_dict
       

class Build_Sinenet_V1(torch.nn.Module):
    ''' 
        Inputs: wav_SBMT, f0_SBM, tau_SBM
        Output: h: S*B*M*D
        1. Apply sinenet on each sub-window within
        2. Stack nlf_SBM, tau_SBM, vuv_SBM
        3. Apply 2 fc, add, batch_norm, relu
    '''
    def __init__(self, params):
        super().__init__()
        self.params = params
        layer_config = self.params["layer_config"]

        self.params["output_dim_seq"] = ['S', 'B', 'M', 'D']
        num_freq  = layer_config['num_freq']
        # assert layer_config['size'] == sine_size + 3
        # D_out = (layer_config['size']) * self.params["input_dim_values"]['M']
        self.params["output_dim_values"] = {'S': self.params["input_dim_values"]['S'], 'B': self.params["input_dim_values"]['B'], 'M': self.params["input_dim_values"]['M'], 'D': layer_config['size']}

        self.sinenet_fn = Build_Sinenet(params)

        self.linear_fn_1 = torch.nn.Linear(num_freq*2, layer_config['size'])
        self.linear_fn_2 = torch.nn.Linear(3, layer_config['size'])
        self.batch_norm = layer_config["batch_norm"]
        if self.batch_norm:
            self.D_index = len(self.params["input_dim_seq"]) - 1
            self.bn_fn = torch.nn.BatchNorm2d(layer_config['size'])
        self.activation_fn = self.params['activation_fn']

    def forward(self, x_dict):
        
        if 'wav_SBMT' in x_dict:
            x = x_dict['wav_SBMT']
        elif 'h' in x_dict:
            x = x_dict['h']
        
        f, nlf = compute_f_nlf(x_dict)
        tau = x_dict['tau_SBM']
        vuv = x_dict['vuv_SBM']
        
        # sinenet
        sin_cos_x = self.sinenet_fn(x, f, tau)  # S*B*M*2K
        y_SBMD_1  = self.linear_fn_1(sin_cos_x) # S*B*M*2K -> S*B*M*D
        # nlf, tau, vuv
        nlf_1 = torch.unsqueeze(nlf, 3)
        tau_1 = torch.unsqueeze(tau, 3)
        vuv_1 = torch.unsqueeze(vuv, 3)
        nlf_tau_vuv = torch.cat([nlf_1, tau_1, vuv_1], 3)
        y_SBMD_2 = self.linear_fn_2(nlf_tau_vuv) # S*B*M*3 -> S*B*M*D

        y_SBMD = y_SBMD_1 + y_SBMD_2
        # Batch Norm
        if self.batch_norm:
            y_SBMD = batch_norm_D_tensor(y_SBMD, self.bn_fn, index_D=self.D_index)

        # ReLU
        h_SBMD = self.activation_fn(y_SBMD)

        y_dict = {'h': h_SBMD}
        return y_dict

class Build_Sinenet_V2(Build_Sinenet_V1):
    ''' Predicted lf0 and tau values
            Inputs: wav_SBMT, f0_SBM, tau_SBM
            Output: y: S*B*M*D, h: S*B*D, D <-- (M*(D+1))
        1. Apply sinenet on each sub-window within 
        2. Apply fc, batch_norm, relu
        3. DNN part: wav_SBMT, nlf_SBM, tau_SBM, vuv_SBM
    '''
    def __init__(self, params):
        super().__init__(params)
        layer_config = self.params["layer_config"]

        DNN_size = layer_config['DNN_size']
        wav_size = self.params["input_dim_values"]['T']
        # assert layer_config['size'] == sine_size + DNN_size

        self.linear_fn_2 = torch.nn.Linear(wav_size+3, DNN_size)

    def forward(self, x_dict):
        
        if 'wav_SBMT' in x_dict:
            x = x_dict['wav_SBMT']
        elif 'h' in x_dict:
            x = x_dict['h']
        
        f, nlf = compute_f_nlf(x_dict)
        tau = x_dict['tau_SBM']
        vuv = x_dict['vuv_SBM']
        
        # sinenet
        sin_cos_x = self.sinenet_fn(x, f, tau)
        y_SBMD = self.linear_fn(sin_cos_x)                             # S*B*M*2K -> S*B*M*D
        # Batch Norm
        if self.batch_norm:
            y_SBMD = batch_norm_D_tensor(y_SBMD, self.bn_fn, index_D=3)

        # ReLU
        h_SBMD = self.activation_fn(y_SBMD)

        # DNN
        nlf_1 = torch.unsqueeze(nlf, 3)
        tau_1 = torch.unsqueeze(tau, 3)
        vuv_1 = torch.unsqueeze(vuv, 3)
        x_nlf_tau_vuv = torch.cat([x, nlf_1, tau_1, vuv_1], 3)
        y_SBMD_2 = self.linear_fn_2(x_nlf_tau_vuv)
        h_SBMD_2 = self.activation_fn(y_SBMD_2)

        h = torch.cat([h_SBMD, h_SBMD_2], 3)

        y_dict = {'h': h}
        return y_dict

class Build_Sinenet_V1_Residual(torch.nn.Module):
    ''' 
        Inputs: wav_SBMT, f0_SBM, tau_SBM
        Output: h: S*B*M*D
        1. Use sinenet to compute sine/cosine matrix
        2. Extract residual x
        2. Append nlf_SBM, tau_SBM, vuv_SBM
        3. Apply fc, batch_norm, relu
    '''
    def __init__(self, params):
        super().__init__()
        self.params = params
        layer_config = self.params["layer_config"]

        self.params["output_dim_seq"] = ['S', 'B', 'M', 'D']
        num_freq  = layer_config['num_freq']
        # assert layer_config['size'] == sine_size + 3
        # D_out = (layer_config['size']) * self.params["input_dim_values"]['M']
        self.params["output_dim_values"] = {'S': self.params["input_dim_values"]['S'], 'B': self.params["input_dim_values"]['B'], 'M': self.params["input_dim_values"]['M'], 'D': layer_config['size']}

        self.sinenet_fn = Build_Sinenet(params)

        self.linear_fn_1 = torch.nn.Linear(self.params["input_dim_values"]['T'], layer_config['size'])
        self.linear_fn_2 = torch.nn.Linear(3, layer_config['size'])
        self.batch_norm = layer_config["batch_norm"]
        if self.batch_norm:
            self.D_index = len(self.params["input_dim_seq"]) - 1
            self.bn_fn = torch.nn.BatchNorm2d(layer_config['size'])
        self.activation_fn = self.params['activation_fn']

    def compute_x_residual(self, x, w_sc):
        '''
        Inputs:
            x: S*B*M*T
            w_sc: # S*B*M*2K*T
        '''
        w_sc_T = torch.transpose(w_sc, 3, 4)      # S*B*M*T*2K
        a_sc_inv = torch.matmul(w_sc, w_sc_T)     # S*B*M*2K*2K
        a_sc = torch.inverse(a_sc_inv)            # S*B*M*2K*2K

        w_sc_x = torch.einsum('sbmkt,sbmt->sbmk', w_sc, x)
        a_sc_w_sc_x = torch.einsum('sbmk,sbmjk->sbmj', w_sc_x, a_sc) # Use j as another k, since a_sc is k*k
        x_sc = torch.einsum('sbmtk,sbmk->sbmt', w_sc_T, a_sc_w_sc_x)

        x_res = x - x_sc
        return x_res

    def forward(self, x_dict):
        
        if 'wav_SBMT' in x_dict:
            x = x_dict['wav_SBMT']
        elif 'h' in x_dict:
            x = x_dict['h']
        
        f, nlf = compute_f_nlf(x_dict)
        tau = x_dict['tau_SBM']
        vuv = x_dict['vuv_SBM']
        
        # sinenet
        w_sc = self.sinenet_fn.construct_w_sin_cos_matrix(f, tau) # S*B*M*2K*T
        x_res = self.compute_x_residual(x, w_sc)
        
        y_SBMD_1  = self.linear_fn_1(x_res) # S*B*M*T -> S*B*M*D
        # nlf, tau, vuv
        nlf_1 = torch.unsqueeze(nlf, 3)
        tau_1 = torch.unsqueeze(tau, 3)
        vuv_1 = torch.unsqueeze(vuv, 3)
        nlf_tau_vuv = torch.cat([nlf_1, tau_1, vuv_1], 3)
        y_SBMD_2 = self.linear_fn_2(nlf_tau_vuv) # S*B*M*3 -> S*B*M*D

        y_SBMD = y_SBMD_1 + y_SBMD_2
        # Batch Norm
        if self.batch_norm:
            y_SBMD = batch_norm_D_tensor(y_SBMD, self.bn_fn, index_D=self.D_index)

        # ReLU
        h_SBMD = self.activation_fn(y_SBMD)

        y_dict = {'h': h_SBMD}
        return y_dict


class Build_Sinenet(torch.nn.Module):
    ''' 
    Inputs: wav_SBMT, f0_SBM, tau_SBM
    Output: sin_cos_x: S*B*M*2K, K=num_freq
    (Optional) output: w_sin_cos_matrix
    '''
    def __init__(self, params):
        super().__init__()
        self.params = params

        self.num_freq = self.params["layer_config"]['num_freq']
        self.win_len  = self.params["input_dim_values"]['T']

        self.t_wav = 1./16000

        self.k_2pi_tensor = self.make_k_2pi_tensor(self.num_freq) # K
        self.n_T_tensor   = self.make_n_T_tensor(self.win_len, self.t_wav)   # T

    def forward(self, x, f, tau):
        sin_cos_matrix = self.construct_w_sin_cos_matrix(f, tau) # S*B*M*2K*T
        sin_cos_x = torch.einsum('sbmkt,sbmt->sbmk', sin_cos_matrix, x) 
        return sin_cos_x

    

    def make_k_2pi_tensor(self, num_freq):
        ''' indices of frequency components '''
        k_vec = numpy.zeros(num_freq)
        for k in range(num_freq):
            k_vec[k] = k + 1
        k_vec = k_vec * 2 * numpy.pi
        k_vec_tensor = torch.tensor(k_vec, dtype=torch.float, requires_grad=False)
        k_vec_tensor = torch.nn.Parameter(k_vec_tensor, requires_grad=False)
        return k_vec_tensor

    def make_n_T_tensor(self, win_len, t_wav):
        ''' indices along time '''
        n_T_vec = numpy.zeros(win_len)
        for n in range(win_len):
            n_T_vec[n] = float(n) * t_wav
        n_T_tensor = torch.tensor(n_T_vec, dtype=torch.float, requires_grad=False)
        n_T_tensor = torch.nn.Parameter(n_T_tensor, requires_grad=False)
        return n_T_tensor

    def compute_deg(self, f, tau):
        ''' Return degree in radian '''
        # Time
        tau_1 = torch.unsqueeze(tau, 3) # S*B*M --> # S*B*M*1
        t = torch.add(self.n_T_tensor, torch.neg(tau_1)) # T + S*B*M*1 -> S*B*M*T

        # Degree in radian
        f_1 = torch.unsqueeze(f, 3) # S*B*M --> # S*B*M*1
        k_2pi_f = torch.mul(self.k_2pi_tensor, f_1) # K + S*B*M*1 -> S*B*M*K
        k_2pi_f_1 = torch.unsqueeze(k_2pi_f, 4) # S*B*M*K -> S*B*M*K*1
        t_1 = torch.unsqueeze(t, 3) # S*B*M*T -> S*B*M*1*T
        deg = torch.mul(k_2pi_f_1, t_1) # S*B*M*K*1, S*B*M*1*T -> S*B*M*K*T
        return deg

    def construct_w_sin_cos_matrix(self, f, tau):
        deg = self.compute_deg(f, tau) # S*B*M*K*T
        s   = torch.sin(deg)             # S*B*M*K*T
        c   = torch.cos(deg)             # S*B*M*K*T
        s_c = torch.cat([s,c], dim=3)    # S*B*M*2K*T
        return s_c

    

        
