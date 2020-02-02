import torch.nn.init

import corpora.reference
import corpora.reference_sentence
from corpora import data
import models
from domain import get_domain
from engines.rnn_reference_engine import RnnReferenceEngine, HierarchicalRnnReferenceEngine
from models.ctx_encoder import *

from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

from utils import set_temporary_default_tensor_type


class RnnReferenceModel(nn.Module):
    corpus_ty = corpora.reference.ReferenceCorpus
    engine_ty = RnnReferenceEngine

    @classmethod
    def add_args(cls, parser):

        parser.add_argument('--nembed_word', type=int, default=128,
                            help='size of word embeddings')
        parser.add_argument('--nhid_rel', type=int, default=64,
                            help='size of the hidden state for the language module')
        parser.add_argument('--nembed_ctx', type=int, default=128,
                            help='size of context embeddings')
        parser.add_argument('--nembed_cond', type=int, default=128,
                            help='size of condition embeddings')
        parser.add_argument('--nhid_lang', type=int, default=128,
                            help='size of the hidden state for the language module')
        parser.add_argument('--nhid_strat', type=int, default=128,
                            help='size of the hidden state for the strategy module')
        parser.add_argument('--nhid_attn', type=int, default=64,
                            help='size of the hidden state for the attention module')
        parser.add_argument('--nhid_sel', type=int, default=64,
                            help='size of the hidden state for the selection module')
        parser.add_argument('--share_attn', action='store_true', default=False,
                            help='share attention modules for selection and language output')

        parser.add_argument('--selection_attention', action='store_true')

    def __init__(self, word_dict, args):
        super(RnnReferenceModel, self).__init__()

        domain = get_domain(args.domain)

        self.word_dict = word_dict
        self.args = args
        self.num_ent = domain.num_ent()

        # define modules:
        self.word_embed = nn.Embedding(len(self.word_dict), args.nembed_word)

        ctx_encoder_ty = models.get_ctx_encoder_type(args.ctx_encoder_type)
        self.ctx_encoder = ctx_encoder_ty(domain, args)

        self.reader = nn.GRU(
            input_size=args.nembed_word,
            hidden_size=args.nhid_lang,
            bias=True)

        self.writer = nn.GRUCell(
            input_size=args.nembed_word,
            hidden_size=args.nhid_lang,
            bias=True)

        # if args.attentive_selection_encoder:
        #     self.selection_attention = nn.Sequential(
        #
        #     )

        self.hid2output = nn.Sequential(
            nn.Linear(args.nhid_lang + args.nembed_ctx, args.nembed_word),
            nn.ReLU(),
            nn.Dropout(args.dropout),
            )

        if args.share_attn:
            self.attn = nn.Sequential(
                nn.Linear(args.nhid_lang + args.nembed_ctx, args.nhid_attn),
                nn.ReLU(),
                nn.Dropout(args.dropout),
                torch.nn.Linear(args.nhid_attn, args.nhid_attn),
                nn.ReLU(),
                nn.Dropout(args.dropout),
                torch.nn.Linear(args.nhid_attn, 1))
        else:
            self.attn = nn.Sequential(
                nn.Linear(args.nhid_lang + args.nembed_ctx, args.nhid_sel),
                nn.ReLU(),
                nn.Dropout(args.dropout))
            self.lang_attn = nn.Sequential(
                torch.nn.Linear(args.nhid_sel, args.nhid_attn),
                nn.ReLU(),
                nn.Dropout(args.dropout),
                torch.nn.Linear(args.nhid_attn, 1))
            self.sel_attn = nn.Sequential(
                torch.nn.Linear(args.nhid_sel, args.nhid_sel),
                nn.ReLU(),
                nn.Dropout(args.dropout),
                torch.nn.Linear(args.nhid_sel, 1))
            self.ref_attn = nn.Sequential(
                torch.nn.Linear(args.nhid_sel, args.nhid_sel),
                nn.ReLU(),
                nn.Dropout(args.dropout),
                torch.nn.Linear(args.nhid_sel, 1))

        if self.args.selection_attention:
            self.ctx_layer = nn.Sequential(
                torch.nn.Linear(args.nembed_ctx, args.nhid_attn),
                # nn.ReLU(),
                # nn.Dropout(args.dropout),
                # torch.nn.Linear(args.nhid_attn, args.nhid_attn),
            )

            self.selection_attention_layer = nn.Sequential(
                # takes nembed_ctx and the output of ctx_layer
                torch.nn.Linear(args.nembed_ctx + args.nhid_attn, args.nhid_attn),
                nn.ReLU(),
                nn.Dropout(args.dropout),
                torch.nn.Linear(args.nhid_attn, 1)
            )


        # tie the weights between reader and writer
        self.writer.weight_ih = self.reader.weight_ih_l0
        self.writer.weight_hh = self.reader.weight_hh_l0
        self.writer.bias_ih = self.reader.bias_ih_l0
        self.writer.bias_hh = self.reader.bias_hh_l0

        self.dropout = nn.Dropout(args.dropout)

        # mask for disabling special tokens when generating sentences
        self.special_token_mask = make_mask(len(word_dict),
            [word_dict.get_idx(w) for w in ['<unk>', 'YOU:', 'THEM:', '<pad>']])

        # init
        self.word_embed.weight.data.uniform_(-args.init_range, args.init_range)
        init_rnn(self.reader, args.init_range)
        init_cont(self.hid2output, args.init_range)
        if args.share_attn:
            init_cont(self.attn, args.init_range)
        else:
            init_cont(self.attn, args.init_range)
            init_cont(self.lang_attn, args.init_range)
            init_cont(self.sel_attn, args.init_range)
            init_cont(self.ref_attn, args.init_range)

    def _zero(self, *sizes):
        h = torch.Tensor(*sizes).fill_(0)
        return Variable(h)

    def flatten_parameters(self):
        self.reader.flatten_parameters()

    def embed_dialogue(self, inpt):
        inpt_emb = self.word_embed(inpt)
        inpt_emb = self.dropout(inpt_emb)
        return inpt_emb

    def reference_resolution(self, ctx_h, outs_emb, ref_inpt):
        if ref_inpt is None:
            return None

        bsz = ctx_h.size(0)

        ref_inpt_old = ref_inpt

        # reshape
        ref_inpt = torch.transpose(ref_inpt, 0, 2).contiguous().view(-1, bsz)
        ref_inpt = ref_inpt.view(-1, bsz).unsqueeze(2)

        ref_inpt = ref_inpt.expand(ref_inpt.size(0), ref_inpt.size(1), outs_emb.size(2))

        # gather indices
        ref_inpt = torch.gather(outs_emb, 0, ref_inpt)

        # reshape
        ref_inpt = ref_inpt.view(3, -1, ref_inpt.size(1), ref_inpt.size(2))

        # this mean pools embeddings for the referent's start and end, as well as the end of the sentence it occurs in
        # take mean
        ref_inpt = torch.mean(ref_inpt, 0)
        
        if self.args.share_attn:
            #ref_inpt = self.ref2input(ref_inpt)

            # reshape ctx_h and ref_inpt
            ref_inpt = ref_inpt.unsqueeze(2).expand(ref_inpt.size(0), ref_inpt.size(1), ctx_h.size(1), ref_inpt.size(2))
            ctx_h = ctx_h.unsqueeze(0).expand(ref_inpt.size(0), ref_inpt.size(1), ref_inpt.size(2), ctx_h.size(-1))

            ref_logit = self.attn(torch.cat([ref_inpt, ctx_h], 3))
        else:
            # reshape ctx_h and ref_inpt
            ref_inpt = ref_inpt.unsqueeze(2).expand(ref_inpt.size(0), ref_inpt.size(1), ctx_h.size(1), ref_inpt.size(2))
            ctx_h = ctx_h.unsqueeze(0).expand(ref_inpt.size(0), ref_inpt.size(1), ref_inpt.size(2), ctx_h.size(-1))

            ref_logit = self.ref_attn(self.attn(torch.cat([ref_inpt, ctx_h], 3)))
        return ref_logit.squeeze(3) 

    def selection(self, ctx_h, outs_emb, sel_idx):
        # outs_emb: length x batch_size x dim

        if self.args.selection_attention:
            # batch_size x entity_dim
            transformed_ctx = self.ctx_layer(ctx_h).mean(1)
            # length x batch_size x dim
            #
            text_and_transformed_ctx = torch.cat([
                transformed_ctx.unsqueeze(0).expand(outs_emb.size(0), -1, -1),
                outs_emb], dim=-1)
            # TODO: add a mask
            # length x batch_size
            attention_logits = self.selection_attention_layer(text_and_transformed_ctx).squeeze(-1)
            attention_probs = attention_logits.softmax(dim=0)
            sel_inpt = torch.einsum("lb,lbd->bd", [attention_probs,outs_emb])
        else:
            sel_idx = sel_idx.unsqueeze(0)
            sel_idx = sel_idx.unsqueeze(2)
            sel_idx = sel_idx.expand(sel_idx.size(0), sel_idx.size(1), outs_emb.size(2))
            sel_inpt = torch.gather(outs_emb, 0, sel_idx)
            sel_inpt = sel_inpt.squeeze(0)
        #  batch_size x hidden
        sel_inpt = sel_inpt.unsqueeze(1)
        # stack alongside the entity embeddings
        sel_inpt = sel_inpt.expand(ctx_h.size(0), ctx_h.size(1), ctx_h.size(2))
        if self.args.share_attn:
            sel_logit = self.attn(torch.cat([sel_inpt, ctx_h], 2))
        else:
            sel_logit = self.sel_attn(self.attn(torch.cat([sel_inpt, ctx_h], 2)))
        return sel_logit.squeeze(2)

    def _forward(self, ctx_h, inpt, ref_inpt, sel_idx, lens=None, lang_h=None, compute_sel_out=False, pack=False):
        bsz = ctx_h.size(0)
        seq_len = inpt.size(0)

        # print('inpt size: {}'.format(inpt.size()))

        dialog_emb = self.embed_dialogue(inpt)
        if pack:
            assert lens is not None
            with set_temporary_default_tensor_type(torch.FloatTensor):
                dialog_emb = pack_padded_sequence(dialog_emb, lens.cpu(), enforce_sorted=False)

        if lang_h is None:
            lang_h = self._zero(1, bsz, self.args.nhid_lang)
        # print('lang_h size: {}'.format(lang_h.size()))
        outs_emb, last_h = self.reader(dialog_emb, lang_h)

        if pack:
            outs_emb, _ = pad_packed_sequence(outs_emb, total_length=seq_len)

        # expand num_ent dimensions to calculate attention scores
        outs_emb_expand = outs_emb.unsqueeze(2).expand(-1, -1, ctx_h.size(1), -1)
        ctx_h_expand = ctx_h.unsqueeze(0).expand(outs_emb.size(0), -1, -1, -1)

        # compute attention for language output
        if self.args.share_attn:
            lang_logit = self.attn(torch.cat([outs_emb_expand, ctx_h_expand], 3))
        else:
            lang_logit = self.lang_attn(self.attn(torch.cat([outs_emb_expand, ctx_h_expand], 3)))
        lang_prob = F.softmax(lang_logit, dim=2).expand(ctx_h_expand.size(0), ctx_h_expand.size(1), ctx_h_expand.size(2), ctx_h_expand.size(3))
        ctx_h_lang = torch.sum(torch.mul(ctx_h_expand, lang_prob), 2)

        # compute language output
        outs = self.hid2output(torch.cat([outs_emb, ctx_h_lang], 2))
        outs = F.linear(outs, self.word_embed.weight)
        outs = outs.view(-1, outs.size(2))

        # compute referents output
        # print('ref_inpt.size(): {}'.format(ref_inpt.size()))
        # print('outs_emb.size(): {}'.format(outs_emb.size()))
        ref_out = self.reference_resolution(ctx_h, outs_emb, ref_inpt)

        if compute_sel_out:
            # compute selection
            # print('sel_idx size: {}'.format(sel_idx.size()))
            sel_out = self.selection(ctx_h, outs_emb, sel_idx)
        else:
            sel_out = None

        return outs, ref_out, sel_out, last_h

    def forward(self, ctx, inpt, ref_inpt, sel_idx, lens):
        ctx_h = self.ctx_encoder(ctx.transpose(0,1))
        outs, ref_out, sel_out, last_h = self._forward(ctx_h, inpt, ref_inpt, sel_idx,
                                                       lens=lens, lang_h=None, compute_sel_out=True, pack=False)
        return outs, ref_out, sel_out

    def read(self, ctx_h, inpt, lang_h, prefix_token='THEM:'):
        # Add a 'THEM:' token to the start of the message
        prefix = self.word2var(prefix_token).unsqueeze(0)
        inpt = torch.cat([prefix, inpt])

        dialog_emb = self.embed_dialogue(inpt)

        lang_hs, lang_h = self.reader(dialog_emb, lang_h)

        return lang_hs, lang_h

    def word2var(self, word):
        x = torch.Tensor(1).fill_(self.word_dict.get_idx(word)).long()
        return Variable(x)

    def write(self, ctx_h, lang_h, max_words, temperature,
              start_token='YOU:', stop_tokens=data.STOP_TOKENS):
        # autoregress starting from start_token
        inpt = self.word2var(start_token)

        outs = [inpt.unsqueeze(0)]
        logprobs = []
        lang_hs = []
        for _ in range(max_words):
            # embed
            inpt_emb = self.embed_dialogue(inpt)
            lang_h = self.writer(inpt_emb, lang_h)
            lang_hs.append(lang_h)

            if self.word_dict.get_word(inpt.data[0]) in stop_tokens:
                break

            # compute attention for language output
            lang_h_expand = lang_h.unsqueeze(1).expand(lang_h.size(0), ctx_h.size(1), lang_h.size(1))
            if self.args.share_attn:
                lang_logit = self.attn(torch.cat([lang_h_expand, ctx_h], 2))
            else:
                lang_logit = self.lang_attn(self.attn(torch.cat([lang_h_expand, ctx_h], 2)))
            lang_prob = F.softmax(lang_logit, dim=1).expand(ctx_h.size(0), ctx_h.size(1), ctx_h.size(2))
            ctx_h_lang = torch.sum(torch.mul(ctx_h, lang_prob), 1)

            # compute language output
            out_emb = self.hid2output(torch.cat([lang_h, ctx_h_lang], 1))
            out = F.linear(out_emb, self.word_embed.weight)

            scores = out.div(temperature)
            scores = scores.sub(scores.max().item()).squeeze(0)

            mask = Variable(self.special_token_mask)
            scores = scores.add(mask)

            prob = F.softmax(scores, dim=0)
            logprob = F.log_softmax(scores, dim=0)
            inpt = prob.multinomial(1).detach()
            outs.append(inpt.unsqueeze(0))
            logprob = logprob.gather(0, inpt)
            logprobs.append(logprob)

        outs = torch.cat(outs, 0)

        # read the output utterance
        _, lang_h = self.read(ctx_h, outs, lang_h.unsqueeze(0))
        lang_h = lang_h.squeeze(0)

        return outs, logprobs, lang_h, torch.cat(lang_hs, 0)


class HierarchicalRnnReferenceModel(RnnReferenceModel):
    corpus_ty = corpora.reference_sentence.ReferenceSentenceCorpus
    engine_ty = HierarchicalRnnReferenceEngine

    @classmethod
    def add_args(cls, parser):
        # args from RnnReferenceModel will be added separately
        pass

    def forward(self, ctx, inpts, ref_inpts, sel_idx, lens):
        # inpts is a list, one item per sentence
        # ref_inpts also a list, one per sentence
        # sel_idx is index into the last sentence in the dialogue

        ctx_h = self.ctx_encoder(ctx.transpose(0,1))
        lang_h = None

        all_outs = []
        all_ref_outs = []

        sel_out = None

        assert len(inpts) == len(ref_inpts)
        for i in range(len(inpts)):
            inpt = inpts[i]
            ref_inpt = ref_inpts[i]
            is_last = i == len(inpts) - 1
            this_lens = lens[i]
            outs, ref_out, sel_out, lang_h = self._forward(
                ctx_h, inpt, ref_inpt, sel_idx, lens=this_lens, lang_h=lang_h, compute_sel_out=is_last, pack=True
            )
            all_outs.append(outs)
            all_ref_outs.append(ref_out)

        assert sel_out is not None

        return all_outs, all_ref_outs, sel_out
