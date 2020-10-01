import sys
from collections import defaultdict
import pdb

import numpy as np
import torch
from torch import optim, autograd
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F
import utils
from dialog import DialogLogger
import domain
from engines import Criterion
import math
from collections import Counter

from nltk.parse import CoreNLPParser, CoreNLPDependencyParser

from models import RnnReferenceModel, HierarchicalRnnReferenceModel
from models.markable_detector import BiLSTM_CRF
from models.rnn_reference_model import State
from models.ctx_encoder import pairwise_differences


class Agent(object):
    """ Agent's interface. """
    def feed_context(self, ctx):
        pass

    def read(self, inpt):
        pass

    def write(self):
        pass

    def choose(self):
        pass

    def update(self, agree, reward, choice):
        pass

    def get_attention(self):
        return None


YOU_TOKEN = 'YOU:'
THEM_TOKEN = 'THEM:'

class RnnAgent(Agent):

    def __init__(self, model: HierarchicalRnnReferenceModel, args, name='Alice', train=False, markable_detector=None):
        super(RnnAgent, self).__init__()
        self.model: HierarchicalRnnReferenceModel = model
        self.markable_detector: BiLSTM_CRF = markable_detector
        self.args = args
        self.name = name
        self.human = False
        self.domain = domain.get_domain(args.domain)
        self.train = train
        self.selection_word_index = self.model.word_dict.get_idx('<selection>')
        if train:
            raise NotImplementedError("fix optimization")
            self.model.train()
            self.opt = optim.RMSprop(
            self.model.parameters(),
            lr=args.rl_lr,
            momentum=self.args.momentum)
            self.all_rewards = []
            self.t = 0
        else:
            self.model.eval()

    def _encode(self, inpt, dictionary):
        encoded = torch.Tensor(dictionary.w2i(inpt)).long().unsqueeze(1)
        return encoded

    def _decode(self, out, dictionary):
        return dictionary.i2w(out.data.squeeze(1).cpu())

    def feed_context(self, context, belief_constructor=None, num_markables_to_force=None,
                     min_num_mentions=0, max_num_mentions=12):
        self.reader_lang_hs = []
        self.writer_lang_hs = []
        self.logprobs = []
        self.sents = []
        self.words = []
        self.context = context
        ctx = torch.Tensor([float(x) for x in context]).float().unsqueeze(0) # add batch size of 1
        self.state: State = self.model.initialize_state(ctx, belief_constructor)
        self.timesteps = 0

        # for use with predict_referents
        self.ref_inpts = []
        self.markables = []
        self.partner_ref_inpts = []
        self.partner_markables = []

        # for use with the belief constructor
        self.ref_outs = []
        self.partner_ref_outs = []
        self.next_mention_outs = [self.model.first_mention(
            self.state,
            num_markables=num_markables_to_force,
            force_next_mention_num_markables=num_markables_to_force is not None,
            min_num_mentions=min_num_mentions,
            max_num_mentions=max_num_mentions,
        )]
        self.is_selection_outs = [self.model.is_selection_prediction(self.state)]
        self.sel_outs = []
        self.extras = []

    def feed_partner_context(self, partner_context):
        pass

    def predict_referents(self, ref_inpt, num_markables):
        ref_beliefs = self.state.make_beliefs('ref', self.timesteps, self.partner_ref_outs, self.ref_outs)
        ref_out = self.model.reference_resolution(
            self.state, self.reader_lang_hs[-1], ref_inpt, num_markables,
            for_self=True, ref_beliefs=ref_beliefs
        )
        self.ref_outs.append(ref_out)
        return ref_out

    def predict_partner_referents(self, partner_ref_inpt, partner_num_markables):
        partner_ref_beliefs = self.state.make_beliefs('partner_ref', self.timesteps, self.partner_ref_outs, self.ref_outs)
        partner_ref_out = self.model.reference_resolution(
            self.state, self.reader_lang_hs[-1], partner_ref_inpt, partner_num_markables,
            for_self=False, ref_beliefs=partner_ref_beliefs
        )
        self.partner_ref_outs.append(partner_ref_out)
        return partner_ref_out

    def update_dot_h(self, ref_inpt, partner_ref_inpt, num_markables, partner_num_markables,
                     ref_tgt=None, partner_ref_tgt=None):
        self.state = self.model._update_dot_h_maybe_multi(
            self.state, self.reader_lang_hs[-1],
            ref_inpt, partner_ref_inpt,
            num_markables, partner_num_markables,
            self.ref_outs[-1], self.partner_ref_outs[-1],
            ref_tgt, partner_ref_tgt,
        )

    def detect_markables(self, utterance_words):
        """-> markables: List, ref_boundaries: List"""
        if utterance_words[0] == THEM_TOKEN:
            utterance_words = [YOU_TOKEN] + utterance_words[1:]
        elif utterance_words[0] == YOU_TOKEN:
            pass
        else:
            utterance_words = [YOU_TOKEN] + utterance_words
        markables = []
        ref_boundaries = []
        for markable, ref_boundary in self.markable_detector.detect_markables(utterance_words):
            markables.append(markable)
            ref_boundaries.append(ref_boundary)
        return markables, ref_boundaries

    def markables_to_tensor(self, ref_boundaries):
        partner_num_markables = torch.LongTensor([len(ref_boundaries)])
        if len(ref_boundaries) > 0:
            # add batch dimension
            return torch.LongTensor(ref_boundaries).unsqueeze(0), partner_num_markables
        else:
            return None, partner_num_markables

    def read(self, inpt_words, dots_mentioned=None, dots_mentioned_per_ref=None,
             num_markables=None,
             start_token=THEM_TOKEN,
             partner_ref_inpt=None, partner_num_markables=None,
             next_num_markables_to_force=None,
             ref_tgt=None, partner_ref_tgt=None,
             detect_markables=False,
             min_num_mentions=0,
             max_num_mentions=12,
             is_selection=None,
             ):
        self.sents.append(Variable(self._encode([start_token] + inpt_words, self.model.word_dict)))
        inpt = self._encode(inpt_words, self.model.word_dict)
        if self.model.args.feed_context_attend:
            raise NotImplementedError("need to detect markables and pass those as dots_mentioned (if this was reading YOU:; currently not)")

        (reader_lang_hs, writer_lang_hs), self.state = self.model.read(
            self.state, Variable(inpt),
            prefix_token=start_token,
            dots_mentioned=dots_mentioned,
            dots_mentioned_per_ref=dots_mentioned_per_ref,
            num_markables=num_markables,
            is_selection=is_selection,
        )
        self.reader_lang_hs.append(reader_lang_hs)
        self.writer_lang_hs.append(writer_lang_hs)

        if detect_markables:
            assert self.markable_detector is not None
            partner_markables, ref_boundaries = self.detect_markables(self._decode(inpt, self.model.word_dict))
            partner_ref_inpt, partner_num_markables = self.markables_to_tensor(ref_boundaries)

            self.ref_inpts.append(None)
            self.markables.append([])
            self.partner_ref_inpts.append(partner_ref_inpt)
            self.partner_markables.append(partner_markables)

        if partner_ref_inpt is not None and partner_num_markables is not None:
            self.predict_partner_referents(partner_ref_inpt, partner_num_markables)
        else:
            self.partner_ref_outs.append(None)
        self.ref_outs.append(None)
        self.words.append(self.model.word2var(start_token).unsqueeze(0))
        self.words.append(Variable(inpt))
        self.update_dot_h(ref_inpt=None, partner_ref_inpt=partner_ref_inpt,
                          num_markables=None, partner_num_markables=partner_num_markables,
                          ref_tgt=ref_tgt, partner_ref_tgt=partner_ref_tgt)
        self.next_mention(lens=torch.LongTensor([reader_lang_hs.size(0)]),
                          num_markables_to_force=next_num_markables_to_force,
                          min_num_mentions=min_num_mentions,
                          max_num_mentions=max_num_mentions,)
        if (self.selection_word_index == inpt).any():
            sel_idx = (self.selection_word_index == inpt.flatten()).nonzero()
            assert len(sel_idx) == 1
            # add one to offset from the start_token
            self.selection(sel_idx[0] + 1)
        self.state = self.state._replace(turn=self.state.turn+1)
        self.timesteps += 1
        self.is_selection()
        #assert (torch.cat(self.words).size(0) == torch.cat(self.lang_hs).size(0))

    def write(self, max_words=100, force_words=None, detect_markables=True, start_token=YOU_TOKEN,
              dots_mentioned=None, dots_mentioned_per_ref=None,
              num_markables=None, ref_inpt=None,
              temperature_override=None,
              # used for oracle beliefs
              ref_tgt=None, partner_ref_tgt=None,
              is_selection=None,
              ):
        temperature = temperature_override if temperature_override is not None else self.args.temperature
        generation_beliefs = self.state.make_beliefs('generation', self.timesteps, self.partner_ref_outs, self.ref_outs)
        outs, logprobs, self.state, (reader_lang_hs, writer_lang_hs), extra = self.model.write(
            self.state, max_words, temperature,
            start_token=start_token,
            force_words=force_words,
            dots_mentioned=dots_mentioned,
            dots_mentioned_per_ref=dots_mentioned_per_ref,
            num_markables=num_markables,
            generation_beliefs=generation_beliefs,
            is_selection=is_selection,
        )
        self.logprobs.extend(logprobs)
        self.reader_lang_hs.append(reader_lang_hs)
        self.writer_lang_hs.append(writer_lang_hs)

        if detect_markables:
            assert self.markable_detector is not None
            markables, ref_boundaries = self.detect_markables(self._decode(outs, self.model.word_dict))
            ref_inpt, num_markables = self.markables_to_tensor(ref_boundaries)

            self.ref_inpts.append(ref_inpt)
            self.markables.append(markables)
            self.partner_ref_inpts.append(None)
            self.partner_markables.append([])

        if ref_inpt is not None and num_markables is not None:
            self.predict_referents(ref_inpt, num_markables)
        else:
            self.ref_outs.append(None)
        self.partner_ref_outs.append(None)
        #self.words.append(self.model.word2var('YOU:').unsqueeze(0))
        self.words.append(outs)
        self.sents.append(torch.cat([self.model.word2var(start_token).unsqueeze(1), outs], 0))
        self.extras.append(extra)
        self.update_dot_h(ref_inpt=ref_inpt, partner_ref_inpt=None,
                          num_markables=num_markables, partner_num_markables=None,
                          ref_tgt=ref_tgt, partner_ref_tgt=partner_ref_tgt)
        self.next_mention(lens=torch.LongTensor([reader_lang_hs.size(0)]),
                          num_markables_to_force=torch.LongTensor([0]),
                          )
        if (self.selection_word_index == outs).any():
            sel_idx = (self.selection_word_index == outs.flatten()).nonzero()
            assert len(sel_idx) == 1
            # add one to offset from the start_token
            self.selection(sel_idx[0] + 1)
        self.state = self.state._replace(turn=self.state.turn+1)
        self.timesteps += 1
        self.is_selection()

        """if self.args.visualize_referents:
            #utterance = self._decode(outs, self.model.word_dict)[1:-1]
            #const_tree = list(self.corenlp_parser.parse(utterance))
            utterance = self._decode(outs, self.model.word_dict)
            ref_inpt = [3, 6, len(utterance) - 1]
            ref_inpt = torch.Tensor(ref_inpt).long().unsqueeze(0).unsqueeze(0)
            ref_out = self.model.reference_resolution(self.ctx_h, lang_hs.unsqueeze(1), ref_inpt)
            pdb.set_trace()"""

        #if not (torch.cat(self.words).size(0) + 1 == torch.cat(self.lang_hs).size(0)):
        #    pdb.set_trace()
        #assert (torch.cat(self.words).size(0) + 1 == torch.cat(self.lang_hs).size(0))
        # remove 'YOU:'
        # outs = outs.narrow(0, 1, outs.size(0) - 1)
        return self._decode(outs, self.model.word_dict)

    def next_mention(self, lens, num_markables_to_force=None, min_num_mentions=0, max_num_mentions=12):
        mention_beliefs = self.state.make_beliefs(
            'mention', self.timesteps, self.partner_ref_outs, self.ref_outs,
        )
        next_mention_out = self.model.next_mention_prediction(
            self.state, self.writer_lang_hs[-1], lens, mention_beliefs,
            num_markables_to_force=num_markables_to_force,
            min_num_mentions=min_num_mentions,
            max_num_mentions=max_num_mentions,
        )
        self.next_mention_outs.append(next_mention_out)
        return next_mention_out

    def is_selection(self):
        self.is_selection_outs.append(self.model.is_selection_prediction(self.state))

    def selection(self, sel_idx):
        selection_beliefs = self.state.make_beliefs(
            'selection', self.timesteps, self.partner_ref_outs, self.ref_outs,
        )
        sel_out = self.model.selection(self.state, self.reader_lang_hs[-1], sel_idx, beliefs=selection_beliefs)
        self.sel_outs.append(sel_out)
        return sel_out

    def _make_idxs(self, sents):
        lens, rev_idxs, hid_idxs = [], [], []
        for sent in sents:
            assert sent.size(1) == 1
            # remove last hidden state
            ln = torch.Tensor(1).fill_(sent.size(0) - 1).long()
            lens.append(ln)
            idx = torch.Tensor(sent.size(0), 1, 1).fill_(-1).long()
            for j in range(idx.size(0)):
                idx[j][0][0] = j if j >= sent.size(0) else sent.size(0) - j - 1
            rev_idxs.append(Variable(idx))
            hid_idxs.append(Variable(ln.view(1, 1, 1)))
        return lens, rev_idxs, hid_idxs

    def _choose(self, sample=False):
        outs_emb = torch.cat(self.reader_lang_hs).unsqueeze(1)
        sel_idx = torch.Tensor(1).fill_(outs_emb.size(0) - 1).long()
        choice_logit = self.model.selection(self.state.ctx_differences, self.state.ctx_h, outs_emb, sel_idx)

        prob = F.softmax(choice_logit, dim=1)
        if sample:
            idx = prob.multinomial(1).detach()
            logprob = F.log_softmax(choice_logit, dim=1).gather(1, idx)
        else:
            _, idx = prob.max(1, keepdim=True)
            logprob = None

        # Pick only your choice
        return idx.item(), prob.gather(1, idx), logprob

    def choose(self):
        if self.args.eps < np.random.rand():
            choice, _, _ = self._choose(sample=False)
        else:
            choice, _, logprob = self._choose(sample=True)
            self.logprobs.append(logprob)

        choice, _, _ = self._choose()
        if self.real_ids:
            choice = self.real_ids[choice]
        return choice

    def update(self, agree, reward, choice=None):
        if not self.train:
            return

        self.t += 1
        if len(self.logprobs) == 0:
            return

        self.all_rewards.append(reward)

        r = (reward - np.mean(self.all_rewards)) / max(1e-4, np.std(self.all_rewards))
        g = Variable(torch.zeros(1, 1).fill_(r))
        rewards = []
        for _ in self.logprobs:
            rewards.insert(0, g)
            g = g * self.args.gamma

        loss = 0
        for lp, r in zip(self.logprobs, rewards):
            loss -= lp * r

        self.opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.args.rl_clip)
        if self.args.visual and self.t % 10 == 0:
            self.model_plot.update(self.t)
            self.agree_plot.update('agree', self.t, int(agree))
            self.reward_plot.update('reward', self.t, reward)
            self.reward_plot.update('partner_reward', self.t, partner_reward)
            self.agree_reward_plot.update('reward', self.t, reward_agree)
            self.agree_reward_plot.update('partner_reward', self.t, partner_reward_agree)
            self.loss_plot.update('loss', self.t, loss.data[0][0])

        self.opt.step()
