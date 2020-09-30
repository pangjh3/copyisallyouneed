import torch
import numpy as np
from torch import nn
import torch.nn.functional as F
import math
import os, time

from transformer import Transformer, SinusoidalPositionalEmbedding, Embedding
from utils import move_to_device
from module import label_smoothed_nll_loss, layer_norm, MonoEncoder
from mips import MIPS, augment_query, l2_to_ip
from data import BOS, EOS, ListsToTensor, _back_to_txt_for_check

class Retriever(nn.Module):
    def __init__(self, vocabs, model, mips, mips_max_norm, mem_pool, mem_feat, num_heads, topk, gpuid):
        super(Retriever, self).__init__()
        self.model = model
        self.mips = mips
        if gpuid >= 0:
            self.mips.to_gpu(gpuid=gpuid)
        self.mips_max_norm = mips_max_norm
        self.mem_pool = mem_pool
        self.mem_feat = mem_feat
        self.num_heads = num_heads
        self.topk = topk
        self.vocabs = vocabs

    @classmethod
    def from_pretrained(cls, num_heads, vocabs, input_dir, nprobe, topk, gpuid, load_response_encoder=False):
        model_args = torch.load(os.path.join(input_dir, 'args'))
        model = MultiProjEncoder.from_pretrained(num_heads, vocabs['src'], model_args, os.path.join(input_dir, 'query_encoder'))
        mips = MIPS.from_built(os.path.join(input_dir, 'mips_index'), nprobe=nprobe)
        mips_max_norm = torch.load(os.path.join(input_dir, 'max_norm.pt'))
        mem_pool = [line.strip().split() for line in open(os.path.join(input_dir, 'candidates.txt')).readlines()]
        mem_feat = torch.load(os.path.join(input_dir, 'feat.pt'))
        retriever = cls(vocabs, model, mips, mips_max_norm, mem_pool, mem_feat, num_heads, topk, gpuid)
        if load_response_encoder:
            another_model = ProjEncoder.from_pretrained(vocabs['tgt'], model_args, os.path.join(input_dir, 'response_encoder'))
            return retriever, another_model
        return retriever

    def work(self, inp, allow_hit):
        src_tokens = inp['src_tokens']
        src_feat, src, src_mask = self.model(src_tokens, return_src=True)
        num_heads, bsz, dim = src_feat.size()
        assert num_heads == self.num_heads
        topk = self.topk
        vecsq = src_feat.view(bsz * num_heads, -1).detach().cpu().numpy()
        #retrieval_start = time.time()
        vecsq = augment_query(vecsq)
        D, I = self.mips.search(vecsq, topk + 1)
        D = l2_to_ip(D, vecsq, self.mips_max_norm) / (self.mips_max_norm * self.mips_max_norm)
        # I, D: (bsz * num_heads x (topk + 1) )
        indices = torch.zeros(topk, num_heads, bsz, dtype=torch.long)
        for i, (Ii, Di) in enumerate(zip(I, D)):
            bid, hid = i % bsz, i // bsz
            tmp_list = []
            for pred, _ in zip(Ii, Di):
                if allow_hit or self.mem_pool[pred]!=inp['tgt_raw_sents'][bid]:
                    tmp_list.append(pred)
            tmp_list = tmp_list[:topk]
            assert len(tmp_list) == topk
            indices[:, hid, bid] = tmp_list
        #retrieval_cost = time.time() - retrieval_start
        #print ('retrieval_cost', retrieval_cost)
        # convert to tensors:
        # all_mem_tokens -> seq_len x ( topk * num_heads * bsz )
        # all_mem_feats -> topk * num_heads * bsz x dim
        all_mem_tokens = []
        all_mem_feats = [] 
        for idx in indices.view(-1).tolist():
            all_mem_tokens.append(self.mem_pool[idx]+[EOS])
            all_mem_feats.append(self.mem_feat[idx])
        all_mem_tokens = ListsToTensor(all_mem_tokens, self.vocabs['tgt'])
        all_mem_feats = torch.stack(all_mem_feats, dim=0).to(src_feat.device).view(-1, num_heads, bsz, dim)
        
        # to avoid GPU OOM issue, truncate the mem to the max. length of 1.5 x src_tokens
        max_mem_len = int(1.5 * src_tokens.shape[0])
        all_mem_tokens = move_to_device(all_mem_tokens[:max_mem_len,:], inp['src_tokens'].device)
        
        # all_mem_scores -> topk x num_heads x bsz
        all_mem_scores = torch.sum(src_feat.unsqueeze(0) * all_mem_feats, dim=-1) / (self.mips_max_norm ** 2)

        mem_ret = {}
        indices = indices.view(-1, bsz).transpose(0, 1).tolist():
        mem_ret['retrieval_raw_sents'] = [ [self.mem_pool[idx] for idx in ind] for ind in indices]
        mem_ret['all_mem_tokens'] = all_mem_tokens
        mem_ret['all_mem_scores'] = all_mem_scores
        return src, src_mask, mem_ret

class MatchingModel(nn.Module):
    def __init__(self, query_encoder, response_encoder):
        super(MatchingModel, self).__init__()
        self.query_encoder = query_encoder
        self.response_encoder = response_encoder

    def forward(self, query, response, label_smoothing=0.):
        ''' query and response: [seq_len, batch_size]
        '''
        _, bsz = query.size()
        
        q = self.query_encoder(query)
        r = self.response_encoder(response)
 
        scores = torch.mm(q, r.t()) # bsz x bsz

        gold = torch.arange(bsz, device=scores.device)
        _, pred = torch.max(scores, -1)
        acc = torch.sum(torch.eq(gold, pred).float()) / bsz

        log_probs = F.log_softmax(scores, -1)
        loss, _ = label_smoothed_nll_loss(log_probs, gold, label_smoothing, sum=True)
        loss = loss / bsz

        return loss, acc, bsz

    def work(self, query, response):
        ''' query and response: [seq_len x batch_size ]
        '''
        _, bsz = query.size()
        q = self.query_encoder(query)
        r = self.response_encoder(response)

        scores = torch.sum(q * r, -1)
        return scores

    def save(self, model_args, output_dir):
        os.makedirs(output_dir, exist_ok=True)
        torch.save(self.query_encoder.state_dict(), os.path.join(output_dir, 'query_encoder'))
        torch.save(self.response_encoder.state_dict(), os.path.join(output_dir, 'response_encoder'))
        torch.save(model_args, os.path.join(output_dir, 'args'))

    @classmethod
    def from_params(cls, vocabs, layers, embed_dim, ff_embed_dim, num_heads, dropout, output_dim):
        query_encoder = ProjEncoder(vocabs['src'], layers, embed_dim, ff_embed_dim, num_heads, dropout, output_dim)
        response_encoder = ProjEncoder(vocabs['tgt'], layers, embed_dim, ff_embed_dim, num_heads, dropout, output_dim)
        model = cls(query_encoder, response_encoder)
        return model
    
    @classmethod
    def from_pretrained(cls, vocabs, input_dir):
        model_args = torch.load(os.path.join(input_dir, 'args'))
        query_encoder = ProjEncoder.from_pretrained(vocabs['src'], model_args, os.path.join(input_dir, 'query_encoder'))
        response_encoder = ProjEncoder.from_pretrained(vocabs['tgt'], model_args, os.path.join(input_dir, 'response_encoder'))
        model = cls(query_encoder, response_encoder)
        return model

class MultiProjEncoder(nn.Module):
    def __init__(self, num_proj_heads, vocab, layers, embed_dim, ff_embed_dim, num_heads, dropout, output_dim):
        super(MultiProjEncoder, self).__init__()
        self.encoder = MonoEncoder(vocab, layers, embed_dim, ff_embed_dim, num_heads, dropout)
        self.proj = nn.Linear(embed_dim, num_proj_heads*output_dim)
        self.num_proj_heads = num_proj_heads
        self.output_dim = output_dim
        self.dropout = dropout
        self.reset_parameters()

    def forward(self, input_ids, batch_first=False, return_src=False):
        if batch_first:
            input_ids = input_ids.t()
        src, src_mask = self.encoder(input_ids) 
        ret = src[0,:,:]
        ret = F.dropout(ret, p=self.dropout, training=self.training)
        ret = self.proj(ret).view(-1, self.num_proj_heads, self.output_dim).transpose(0, 1)
        ret = layer_norm(F.dropout(ret, p=self.dropout, training=self.training))
        if return_src:
            return ret, src, src_mask
        return ret

    @classmethod
    def from_pretrained_projencoder(cls, num_proj_heads, vocab, model_args, ckpt):
        model = cls(num_proj_heads, vocab, model_args.layers, model_args.embed_dim, model_args.ff_embed_dim, model_args.num_heads, model_args.dropout, model_args.output_dim)
        state_dict = torch.load(ckpt, map_location='cpu')
        model.encoder.load_state_dict({k[len('encoder.'):]:v for k,v in x.items() if k.startswith('encoder.')})
        weight = state_dict['proj.weight'].repeat(num_proj_heads, 1)
        bias = state_dict['proj.weight'].repeat(num_proj_heads)
        model.proj.weight = nn.Parameter(weight)
        model.proj.bias = nn.Parameter(bias)
        return model

class ProjEncoder(nn.Module):
    def __init__(self, vocab, layers, embed_dim, ff_embed_dim, num_heads, dropout, output_dim):
        super(ProjEncoder, self).__init__()
        self.encoder = MonoEncoder(vocab, layers, embed_dim, ff_embed_dim, num_heads, dropout)
        self.proj = nn.Linear(embed_dim, output_dim)
        self.dropout = dropout
        self.reset_parameters()

    def reset_parameters(self):
        nn.init.normal_(self.proj.weight, std=0.02)
        nn.init.constant_(self.proj.bias, 0.)

    def forward(self, input_ids, batch_first=False, return_src=False):
        if batch_first:
            input_ids = input_ids.t()
        src, src_mask = self.encoder(input_ids) 
        ret = src[0,:,:]
        ret = F.dropout(ret, p=self.dropout, training=self.training)
        ret = self.proj(ret)
        ret = layer_norm(F.dropout(ret, p=self.dropout, training=self.training))
        if return_src:
            return ret, src, src_mask
        return ret

    @classmethod
    def from_pretrained(cls, vocab, model_args, ckpt):
        model = cls(vocab, model_args.layers, model_args.embed_dim, model_args.ff_embed_dim, model_args.num_heads, model_args.dropout, model_args.output_dim)
        model.load_state_dict(torch.load(ckpt, map_location='cpu'))
        return model
