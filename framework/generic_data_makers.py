################################################################################
#
#  Copyright 2014-2016 Eric Lacombe <eric.lacombe@security-labs.org>
#
################################################################################
#
#  This file is part of fuddly.
#
#  fuddly is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  fuddly is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with fuddly. If not, see <http://www.gnu.org/licenses/>
#
################################################################################

import sys
import random
import array
import time
import itertools
import binascii
import subprocess
from copy import *

from framework.data_model import *
from framework.tactics_helpers import *
from framework.fuzzing_primitives import *
from framework.basic_primitives import *
from framework.value_types import *
from framework.data_model_helpers import GENERIC_ARGS

from framework.global_resources import *

tactics = Tactics()


#######################
# STATEFUL DISRUPTORS #
#######################


@disruptor(tactics, dtype="tWALK", weight=1,
           gen_args = GENERIC_ARGS,
           args={'path': ('graph path regexp to select nodes on which' \
                          ' the disruptor should apply', None, str),
                 'nt_only': ('walk through non-terminal nodes only', False, bool),
                 'singleton': ('consume also terminal nodes with only one possible value', False, bool),
                 'fix': ('fix constraints while walking', True, bool)})
class sd_iter_over_data(StatefulDisruptor):
    '''
    Walk through the provided data and for each visited node, iterates
    over the allowed values (with respect to the data model).
    Note: *no alteration* is performed by this disruptor.
    '''
    def setup(self, dm, user_input):
        return True

    def set_seed(self, prev_data):
        if prev_data.node is None:
            prev_data.add_info('DONT_PROCESS_THIS_KIND_OF_DATA')
            return prev_data

        prev_data.node.make_finite(all_conf=True, recursive=True)

        if self.nt_only:
            consumer = NonTermVisitor()
        else:
            consumer = BasicVisitor(consume_also_singleton=self.singleton)
        consumer.set_node_interest(path_regexp=self.path)
        self.modelwalker = ModelWalker(prev_data.node, consumer, max_steps=self.max_steps, initial_step=self.init)
        self.walker = iter(self.modelwalker)


    def disrupt_data(self, dm, target, data):
        try:
            rnode, consumed_node, orig_node_val, idx = next(self.walker)
        except StopIteration:
            data.make_unusable()
            self.handover()
            return data

        data.add_info('model walking index: {:d}'.format(idx))
        data.add_info('current node:     %s' % self.modelwalker.consumed_node_path)

        if self.clone_node:
            exported_node = Node(rnode.name, base_node=rnode, new_env=True)
        else:
            exported_node = rnode

        if self.fix:
            exported_node.unfreeze(recursive=True, reevaluate_constraints=True)
            exported_node.freeze()
            data.add_info('fix constraints (if any)')

        data.update_from_node(exported_node)

        return data



@disruptor(tactics, dtype="tTYPE", weight=1,
           gen_args = GENERIC_ARGS,
           args={'path': ('graph path regexp to select nodes on which' \
                          ' the disruptor should apply', None, str),
                 'order': ('when set to True, the fuzzing order is strictly guided ' \
                           'by the data structure. Otherwise, fuzz weight (if specified ' \
                           'in the data model) is used for ordering', False, bool),
                 'deep': ('when set to True, if a node structure has changed, the modelwalker ' \
                          'will reset its walk through the children nodes', True, bool)})
class sd_fuzz_typed_nodes(StatefulDisruptor):
    '''
    Perform alterations on typed nodes (one at a time) according to
    its type and various complementary information (such as size,
    allowed values, ...).
    '''
    def setup(self, dm, user_input):
        return True

    def set_seed(self, prev_data):
        if prev_data.node is None:
            prev_data.add_info('DONT_PROCESS_THIS_KIND_OF_DATA')
            return prev_data

        prev_data.node.make_finite(all_conf=True, recursive=True)

        self.consumer = TypedNodeDisruption(max_runs_per_node=self.max_runs_per_node,
                                            min_runs_per_node=self.min_runs_per_node,
                                            respect_order=self.order)
        self.consumer.need_reset_when_structure_change = self.deep
        self.consumer.set_node_interest(path_regexp=self.path)
        self.modelwalker = ModelWalker(prev_data.node, self.consumer, max_steps=self.max_steps, initial_step=self.init)
        self.walker = iter(self.modelwalker)

        self.max_runs = None
        self.current_node = None
        self.run_num = None

    def disrupt_data(self, dm, target, data):
        try:
            rnode, consumed_node, orig_node_val, idx = next(self.walker)
        except StopIteration:
            data.make_unusable()
            self.handover()
            return data

        new_max_runs = self.consumer.max_nb_runs_for(consumed_node)
        if self.max_runs != new_max_runs or self.current_node != consumed_node:
            self.current_node = consumed_node
            self.max_runs = new_max_runs
            self.run_num = 1
        else:
            self.run_num +=1

        data.add_info('model walking index: {:d}'.format(idx))        
        data.add_info(' |_ run: {:d} / {:d} (max)'.format(self.run_num, self.max_runs))
        data.add_info('current fuzzed node:     %s' % self.modelwalker.consumed_node_path)
        data.add_info(' |_ value type:         %s' % consumed_node.cc.get_value_type())
        data.add_info(' |_ original node value: %s (ascii: %s)' % \
                      (binascii.b2a_hex(orig_node_val), orig_node_val))
        data.add_info(' |_ corrupt node value:  %s (ascii: %s)' % \
                      (binascii.b2a_hex(consumed_node.to_bytes()),
                      consumed_node.to_bytes()))

        if self.clone_node:
            exported_node = Node(rnode.name, base_node=rnode, new_env=True)
            data.update_from_node(exported_node)
        else:
            data.update_from_node(rnode)

        return data



@disruptor(tactics, dtype="tALT", weight=1,
           gen_args = GENERIC_ARGS,
           args={'conf': ("change the configuration, with the one provided (by name), of " \
                          "all subnodes fetched by @path, one-by-one. [default value is set " \
                          "dynamically with the first-found existing alternate configuration]",
                          None, (str,list,tuple))})
class sd_switch_to_alternate_conf(StatefulDisruptor):
    '''
    Switch the configuration of each node, one by one, with the
    provided alternate configuration.
    '''
    def setup(self, dm, user_input):
        available_confs = dm.get_available_confs()

        all_alternate_confs = copy.copy(available_confs)
        if not all_alternate_confs:
            return False

        self.confs_list = None

        if self.conf is None:
            self.confs_list = all_alternate_confs
        else:
            if isinstance(self.conf, (tuple, list)):
                for c in self.conf:
                    if c in all_alternate_confs:
                        ok = True
                        break
                else:
                    ok = False
                if ok:
                    self.confs_list = self.conf
                else:
                    return False
            elif self.conf not in all_alternate_confs:
                return False
            else:
                self.confs_list = [self.conf]
            
        return True

    def set_seed(self, prev_data):
        if prev_data.node is None:
            prev_data.add_info('DONT_PROCESS_THIS_KIND_OF_DATA')
            return prev_data

        self.consumer = AltConfConsumer(max_runs_per_node=self.max_runs_per_node,
                                        min_runs_per_node=self.min_runs_per_node,
                                        respect_order=False)
        self.consumer.set_node_interest(owned_confs=self.confs_list)
        self.modelwalker = ModelWalker(prev_data.node, self.consumer, max_steps=self.max_steps, initial_step=self.init)
        self.walker = iter(self.modelwalker)

        self.max_runs = None
        self.current_node = None
        self.run_num = None


    def disrupt_data(self, dm, target, data):

        try:
            rnode, consumed_node, orig_node_val, idx = next(self.walker)
        except StopIteration:
            data.make_unusable()
            self.handover()
            return data

        new_max_runs = self.consumer.max_nb_runs_for(consumed_node)
        if self.max_runs != new_max_runs or self.current_node != consumed_node:
            self.current_node = consumed_node
            self.max_runs = new_max_runs
            self.run_num = 1
        else:
            self.run_num +=1

        data.add_info('model walking index: {:d}'.format(idx))        
        data.add_info(' |_ run: {:d} / {:d} (max)'.format(self.run_num, self.max_runs))
        data.add_info('current node with alternate conf: %s' % self.modelwalker.consumed_node_path)
        data.add_info(' |_ associated value: %s' % repr(consumed_node.to_bytes()))
        data.add_info(' |_ original node value: %s' % orig_node_val)

        if self.clone_node:
            exported_node = Node(rnode.name, base_node=rnode, new_env=True)
            data.update_from_node(exported_node)
        else:
            data.update_from_node(rnode)

        return data


@disruptor(tactics, dtype="tSEP", weight=1,
           gen_args = GENERIC_ARGS,
           args={'path': ('graph path regexp to select nodes on which' \
                          ' the disruptor should apply', None, str),
                 'order': ('when set to True, the fuzzing order is strictly guided ' \
                           'by the data structure. Otherwise, fuzz weight (if specified ' \
                           'in the data model) is used for ordering', False, bool),
                 'deep': ('when set to True, if a node structure has changed, the modelwalker ' \
                          'will reset its walk through the children nodes', True, bool)})
class sd_fuzz_separator_nodes(StatefulDisruptor):
    '''
    Perform alterations on separators (one at a time). Each time a
    separator is encountered in the provided data, it will be replaced
    by another separator picked from the ones existing within the
    provided data.
    '''
    def setup(self, dm, user_input):
        return True

    def set_seed(self, prev_data):
        if prev_data.node is None:
            prev_data.add_info('DONT_PROCESS_THIS_KIND_OF_DATA')
            return prev_data

        prev_data.node.get_value()

        ic = dm.NodeInternalsCriteria(mandatory_attrs=[dm.NodeInternals.Separator])
        sep_list = set(map(lambda x: x.to_bytes(), prev_data.node.get_reachable_nodes(internals_criteria=ic)))
        sep_list = list(sep_list)
        prev_data.add_info('separators found: {!r}'.format(sep_list))

        prev_data.node.make_finite(all_conf=True, recursive=True)

        self.consumer = SeparatorDisruption(max_runs_per_node=self.max_runs_per_node,
                                            min_runs_per_node=self.min_runs_per_node,
                                            respect_order=self.order,
                                            separators=sep_list)
        self.consumer.need_reset_when_structure_change = self.deep
        self.consumer.set_node_interest(path_regexp=self.path)
        self.modelwalker = ModelWalker(prev_data.node, self.consumer, max_steps=self.max_steps, initial_step=self.init)
        self.walker = iter(self.modelwalker)

        self.max_runs = None
        self.current_node = None
        self.run_num = None

    def disrupt_data(self, dm, target, data):
        try:
            rnode, consumed_node, orig_node_val, idx = next(self.walker)
        except StopIteration:
            data.make_unusable()
            self.handover()
            return data

        new_max_runs = self.consumer.max_nb_runs_for(consumed_node)
        if self.max_runs != new_max_runs or self.current_node != consumed_node:
            self.current_node = consumed_node
            self.max_runs = new_max_runs
            self.run_num = 1
        else:
            self.run_num +=1

        data.add_info('model walking index: {:d}'.format(idx))        
        data.add_info(' |_ run: {:d} / {:d} (max)'.format(self.run_num, self.max_runs))
        data.add_info('current fuzzed separator:     %s' % self.modelwalker.consumed_node_path)
        data.add_info(' |_ value type:         %s' % consumed_node.cc.get_value_type())
        data.add_info(' |_ original separator: %s (ascii: %s)' % \
                      (binascii.b2a_hex(orig_node_val), orig_node_val))
        data.add_info(' |_ replaced by:        %s (ascii: %s)' % \
                      (binascii.b2a_hex(consumed_node.to_bytes()),
                      consumed_node.to_bytes()))

        if self.clone_node:
            exported_node = Node(rnode.name, base_node=rnode, new_env=True)
            data.update_from_node(exported_node)
        else:
            data.update_from_node(rnode)

        return data



@disruptor(tactics, dtype="tSTRUCT", weight=1,
           gen_args={'init': ('make the model walker ignore all the steps until the provided one', 1, int),
                     'max_steps': ('maximum number of steps (-1 means until the end)', -1, int) },
           args={'path': ('graph path regexp to select nodes on which' \
                          ' the disruptor should apply', None, str),
                 'deep': ('if True, enable corruption of minimum and maxium amount of non-terminal nodes',
                          False, bool) })
class sd_struct_constraints(StatefulDisruptor):
    '''
    For each node associated to existence constraints or quantity
    constraints, alter the constraint, one at a time, after each call
    to this disruptor.

    If `deep` is set, enable new structure corruption cases, based on
    the minimum and maximum amount of non-terminal nodes (within the
    input data) specified in the data model.
    '''
    def setup(self, dm, user_input):
        return True

    def set_seed(self, prev_data):
        if prev_data.node is None:
            prev_data.add_info('CANNOT PROCESS THIS KIND OF DATA')
            return prev_data

        self.seed = prev_data.node
        self.seed.make_finite(all_conf=True, recursive=True)
        self.seed.freeze()

        self.idx = 0

        ic_exist_cst = NodeInternalsCriteria(required_csts=[SyncScope.Existence])
        ic_qty_cst = NodeInternalsCriteria(required_csts=[SyncScope.Qty])
        ic_size_cst = NodeInternalsCriteria(required_csts=[SyncScope.Size])
        ic_minmax_cst = NodeInternalsCriteria(node_kinds=[NodeInternals_NonTerm])

        self.exist_cst_nodelist = self.seed.get_reachable_nodes(internals_criteria=ic_exist_cst, path_regexp=self.path,
                                                                ignore_fstate=True)
        # print('\n*** NOT FILTERED nodes')
        # for n in self.exist_cst_nodelist:
        #     print(' |_ ' + n.name)
        # self.exist_cst_nodelist = self.seed.filter_out_entangled_nodes(self.exist_cst_nodelist)
        # print('\n*** FILTERED nodes')
        # for n in self.exist_cst_nodelist:
        #     print(' |_ ' + n.name)
        nodelist = copy.copy(self.exist_cst_nodelist)
        for n in nodelist:
            if n.get_path_from(self.seed) is None:
                self.exist_cst_nodelist.remove(n)

        self.qty_cst_nodelist_1 = self.seed.get_reachable_nodes(internals_criteria=ic_qty_cst, path_regexp=self.path,
                                                                ignore_fstate=True)
        # self.qty_cst_nodelist_1 = self.seed.filter_out_entangled_nodes(self.qty_cst_nodelist_1)
        nodelist = copy.copy(self.qty_cst_nodelist_1)
        for n in nodelist:
            if n.get_path_from(self.seed) is None:
                self.qty_cst_nodelist_1.remove(n)

        self.qty_cst_nodelist_2 = copy.copy(self.qty_cst_nodelist_1)

        self.size_cst_nodelist_1 = self.seed.get_reachable_nodes(internals_criteria=ic_size_cst, path_regexp=self.path,
                                                               ignore_fstate=True)
        nodelist = copy.copy(self.size_cst_nodelist_1)
        for n in nodelist:
            if n.get_path_from(self.seed) is None:
                self.size_cst_nodelist_1.remove(n)
        self.size_cst_nodelist_2 = copy.copy(self.size_cst_nodelist_1)

        if self.deep:
            minmax_cst_nodelist = self.seed.get_reachable_nodes(internals_criteria=ic_minmax_cst, path_regexp=self.path,
                                                                ignore_fstate=True)
            self.minmax_cst_nodelist_1 = set()

            for n in minmax_cst_nodelist:
                for sn in n.subnodes_set:
                    minmax = n.get_subnode_minmax(sn)
                    if minmax is not None:
                        mini, maxi = minmax
                        self.minmax_cst_nodelist_1.add((sn, mini, maxi))

            nodedesclist = copy.copy(self.minmax_cst_nodelist_1)
            for n_desc in nodedesclist:
                n, mini, maxi = n_desc
                if n.get_path_from(self.seed) is None:
                    self.minmax_cst_nodelist_1.remove((n, mini, maxi))

            self.minmax_cst_nodelist_2 = copy.copy(self.minmax_cst_nodelist_1)

        else:
            self.minmax_cst_nodelist_1 = self.minmax_cst_nodelist_2 = []

        self.max_runs = len(self.exist_cst_nodelist) + 2*len(self.size_cst_nodelist_1) + \
                        2*len(self.qty_cst_nodelist_1) + 2*len(self.minmax_cst_nodelist_1)
        

    def disrupt_data(self, dm, target, data):

        stop = False
        if self.idx == 0:
            step_idx = self.init-1
        else:
            step_idx = self.idx

        while self.idx <= step_idx:
            if self.exist_cst_nodelist:
                consumed_node = self.exist_cst_nodelist.pop()
                if self.idx == step_idx:
                    self.seed.env.add_node_to_corrupt(consumed_node, corrupt_type=Node.CORRUPT_EXIST_COND)
                    op_performed = 'existence condition switched'
            elif self.qty_cst_nodelist_1:
                consumed_node = self.qty_cst_nodelist_1.pop()
                if self.idx == step_idx:
                    self.seed.env.add_node_to_corrupt(consumed_node, corrupt_type=Node.CORRUPT_QTY_SYNC,
                                                      corrupt_op=lambda x: x+1)
                    op_performed = 'increase quantity constraint by 1'
            elif self.qty_cst_nodelist_2:
                consumed_node = self.qty_cst_nodelist_2.pop()
                if self.idx == step_idx:
                    self.seed.env.add_node_to_corrupt(consumed_node, corrupt_type=Node.CORRUPT_QTY_SYNC,
                                                      corrupt_op=lambda x: max(x-1, 0))
                    op_performed = 'decrease quantity constraint by 1'
            elif self.size_cst_nodelist_1:
                consumed_node = self.size_cst_nodelist_1.pop()
                if self.idx == step_idx:
                    self.seed.env.add_node_to_corrupt(consumed_node, corrupt_type=Node.CORRUPT_SIZE_SYNC,
                                                      corrupt_op=lambda x: x+1)
                    op_performed = 'increase size constraint by 1'
            elif self.size_cst_nodelist_2:
                consumed_node = self.size_cst_nodelist_2.pop()
                if self.idx == step_idx:
                    self.seed.env.add_node_to_corrupt(consumed_node, corrupt_type=Node.CORRUPT_SIZE_SYNC,
                                                      corrupt_op=lambda x: max(x-1, 0))
                    op_performed = 'decrease size constraint by 1'
            elif self.deep and self.minmax_cst_nodelist_1:
                consumed_node, mini, maxi = self.minmax_cst_nodelist_1.pop()
                if self.idx == step_idx:
                    new_mini = max(0, mini-1)
                    self.seed.env.add_node_to_corrupt(consumed_node, corrupt_type=Node.CORRUPT_NODE_QTY,
                                                      corrupt_op=lambda x, y: (new_mini, new_mini))
                    op_performed = "set node amount to its minimum minus one"
            elif self.deep and self.minmax_cst_nodelist_2:
                consumed_node, mini, maxi = self.minmax_cst_nodelist_2.pop()
                if self.idx == step_idx:
                    new_maxi = (maxi+1)
                    self.seed.env.add_node_to_corrupt(consumed_node, corrupt_type=Node.CORRUPT_NODE_QTY,
                                                      corrupt_op=lambda x, y: (new_maxi, new_maxi))
                    op_performed = "set node amount to its maximum plus one"
            else:
                stop = True
                break

            self.idx += 1

        if stop or (self.idx > self.max_steps and self.max_steps != -1):
            data.make_unusable()
            self.handover()
            return data

        corrupted_seed = Node(self.seed.name, base_node=self.seed, ignore_frozen_state=False, new_env=True)
        self.seed.env.remove_node_to_corrupt(consumed_node)

        corrupted_seed.unfreeze(recursive=True, reevaluate_constraints=True)
        corrupted_seed.freeze()

        data.add_info('sample index: {:d}'.format(self.idx))
        data.add_info(' |_ run: {:d} / {:d}'.format(self.idx, self.max_runs))
        data.add_info('current fuzzed node:    {:s}'.format(consumed_node.get_path_from(self.seed)))
        data.add_info(' |_ {:s}'.format(op_performed))

        data.update_from_node(corrupted_seed)

        return data




########################
# STATELESS DISRUPTORS #
########################


@disruptor(tactics, dtype="EXT", weight=1,
           args={'cmd': ('the command', None, (list,tuple,str)),
                 'file_mode': ('if True the data will be provided through ' \
                               'a file to the external program, otherwise it ' \
                               'will be provided on the command line directly', True, bool),
                 'path': ('graph path regexp to select nodes on which ' \
                          'the disruptor should apply', None, str)})
class d_call_external_program(Disruptor):
    '''
    Call an external program to deal with the data.
    '''
    def setup(self, dm, user_input):
        if self._get_cmd() is None:
            if self.cmd is None:
                print("\n*** ERROR: A command should be provided!")
                return False

        return True

    def _get_cmd(self):
        return self.cmd

    def disrupt_data(self, dm, target, prev_data):

        if self.path:
            node = prev_data.node.get_node_by_path(path_regexp=self.path)
            if node is None:
                prev_data.add_info('INVALID INPUT')
                return prev_data
            raw_data = node.to_bytes()
        else:
            node = None
            raw_data = prev_data.to_bytes()

        cmd = self._get_cmd()

        if isinstance(cmd, list):
            cmd_repr = ' '.join(cmd)
        else:
            cmd_repr = cmd

        # provide prev_data through a file
        if self.file_mode:
            dm = prev_data.get_data_model()
            if dm:
                file_extension = dm.file_extension
            else:
                file_extension = 'bin'

            filename = os.path.join(workspace_folder, 'EXT_file.' + file_extension)
            with open(filename, 'wb') as f:
                f.write(raw_data)

            prev_data.add_info("Execute command: {:s}".format(cmd_repr + ' ' + filename))
            arg = filename

        # provide prev_data on the command line
        else:
            prev_data.add_info("Execute command: {:s}".format(cmd_repr + ' ' + str(prev_data)[:20] + '...'))
            arg = raw_data

        if isinstance(cmd, list):
            cmd = list(cmd)
            cmd.append(arg)
        else:
            cmd = cmd + ' ' + arg
            cmd = cmd.split()

        try:
            out_val = subprocess.check_output(cmd)
        except subprocess.CalledProcessError as e:
            prev_data.add_info("/!\\ Error encountered while executing external command!")
            return prev_data

        if node is None:
            prev_data.update_from_str_or_bytes(out_val)
        else:
            node.set_values(val_list=[out_val])
            node.get_value()

        return prev_data


@disruptor(tactics, dtype="STRUCT", weight=1,
           args={'path': ('graph path regexp to select nodes on which ' \
                          'the disruptor should apply', None, str)})
class d_fuzz_model_structure(Disruptor):
    '''
    Disrupt the data model structure (replace ordered sections by
    unordered ones).
    '''
    def setup(self, dm, user_input):
        return True

    def disrupt_data(self, dm, target, prev_data):
        if prev_data.node:
            fuzz_data_tree(prev_data.node, self.path)
        else:
            prev_data.add_info('DONT_PROCESS_THIS_KIND_OF_DATA')

        return prev_data


@disruptor(tactics, dtype="ALT", weight=1,
           args={'conf': ("change the configuration, with the one provided (by name), of " \
                          "all subnodes fetched by @path, one-by-one. [default value is set " \
                          "dynamically with the first-found existing alternate configuration]",
                          None, str),
                 'path': ('graph path regexp to select nodes on which ' \
                          'the disruptor should apply', None, str),
                 'recursive': ('does the reachable nodes from the selected ' \
                               'ones need also to be changed?', True, bool)})
class d_switch_to_alternate_conf(Disruptor):
    '''
    Switch to an alternate configuration.
    '''
    def setup(self, dm, user_input):
        self.available_confs = dm.get_available_confs()

        if self.available_confs:
            self.conf_fallback = self.available_confs[0]
        else:
            self.conf_fallback = None

        if self.conf is None:
            self.conf = self.conf_fallback
            self.provided_alt = False
        else:
            self.provided_alt = True

        if self.conf in self.available_confs:
            self.existing_conf = True
        else:
            self.existing_conf = False

        return True


    def disrupt_data(self, dm, target, prev_data):
        if prev_data.node:
            
            # try to get more specific default conf
            if not self.provided_alt and self.available_confs:
                confs = prev_data.node.gather_alt_confs()
                if confs:
                    self.conf_fallback = confs.pop()
                    self.conf = self.conf_fallback
                    self.provided_alt = True
                    self.existing_conf = True

            if self.provided_alt and not self.existing_conf:
                prev_data.add_info("NO ALTERNATE CONF '%s' AVAILABLE" % str(self.conf))
                return prev_data

            if self.conf_fallback is None:
                prev_data.add_info("NO ALTERNATE CONF AVAILABLE")
                return prev_data

            prev_data.add_info("ALTERNATE CONF '%s' USED" % str(self.conf))

            prev_data.node.unfreeze_all()
            prev_data.node.set_current_conf(self.conf, recursive=self.recursive, root_regexp=self.path)

            prev_data.node.get_value()

        else:
            prev_data.add_info('DONT_PROCESS_THIS_KIND_OF_DATA')

        return prev_data


@disruptor(tactics, dtype="SIZE", weight=4,
           args={'sz': ("truncate the data (or part of the data) to the provided size", 10, int),
                 'path': ('graph path regexp to select nodes on which ' \
                          'the disruptor should apply', None, str)})
class d_max_size(Disruptor):
    '''
    Truncate the data (or part of the data) to the provided size.
    '''

    def setup(self, dm, user_input):
        return True


    def disrupt_data(self, dm, target, prev_data):

        if prev_data.node:
            if self.path is not None:
                node = prev_data.node.get_node_by_path(self.path)
                if node is None:
                    node = prev_data.node
            else:
                node = prev_data.node

            val = node.to_bytes()
            orig_len = len(val)
            prev_data.add_info('orig node length: %d' % orig_len)
            
            if self.sz >= 0:
                node.set_values([val[:min(self.sz, orig_len)]])
                prev_data.add_info('right truncation')
            else:
                self.sz = - self.sz
                node.set_values([val[orig_len - min(self.sz, orig_len):]])
                prev_data.add_info('left truncation')

            prev_data.add_info('new node length: %d' % min(self.sz, orig_len))

            ret = prev_data

        else:
            val = prev_data.to_bytes()
            orig_len = len(val)
            prev_data.add_info('orig data length: %d' % orig_len)

            if self.sz >= 0:
                new_val = val[:min(self.sz, orig_len)]
                prev_data.add_info('right truncation')
            else:
                self.sz = - self.sz
                new_val = val[orig_len - min(self.sz, orig_len):]
                prev_data.add_info('left truncation')

            prev_data.add_info('new data length: %d' % len(new_val))

            prev_data.update_from_str_or_bytes(new_val)
            ret = prev_data

        return ret



@disruptor(tactics, dtype="C", weight=4,
           args={'nb': ('apply corruption on @nb Nodes fetched randomly within the data model', 2, int),
                 'path': ('graph path regexp to select nodes on which ' \
                          'the disruptor should apply', None, str),
                 'new_val': ('if provided change the selected byte with the new one', None, str),
                 'ascii': ('enforce all outputs to be ascii 7bits', False, bool)})
class d_corrupt_node_bits(Disruptor):
    '''
    Corrupt bits on some nodes of the data model.
    '''
    def setup(self, dm, user_input):
        return True


    def disrupt_data(self, dm, target, prev_data):
        if prev_data.node:
            prev_data.node.get_value()

            c = NodeInternalsCriteria(mandatory_attrs=[NodeInternals.Mutable],
                                      node_kinds=[NodeInternals_TypedValue])
            l = prev_data.node.get_reachable_nodes(path_regexp=self.path,
                                                   internals_criteria=c)
            if not l:
                prev_data.add_info('INVALID INPUT')
                return prev_data

            if self.nb > 0:
                try:
                    l = random.sample(l, self.nb)
                except ValueError:
                    prev_data.add_info('Only one Node (Terminal) has been found!')
                    l = random.sample(l, 1)

            for i in l:
                val = i.to_bytes()
                prev_data.add_info('current fuzzed node: %s' % i.get_path_from(prev_data.node))
                prev_data.add_info('orig data: %s' % repr(val))

                if self.new_val is None:
                    if val != b'':
                        val = corrupt_bits(val, n=1, ascii=self.ascii)
                        prev_data.add_info('corrupted data: %s' % repr(val))
                    else:
                        prev_data.add_info('Nothing to corrupt!')
                else:
                    val = self.new_val
                    prev_data.add_info('corrupted data: %s' % repr(val))

                i.set_values(val_list=[val])
                i.get_value()

            ret = prev_data

        else:
            new_val = corrupt_bits(prev_data.to_bytes(), ascii=self.ascii)
            prev_data.update_from_str_or_bytes(new_val)
            prev_data.add_info('Corruption performed on a byte string as no Node is available')
            ret = prev_data

        return ret


@disruptor(tactics, dtype="Cp", weight=4,
           args={'idx': ('byte index to be corrupted (from 1 to data length)', 1, int),
                 'new_val': ('if provided change the selected byte with the new one', None, str),
                 'ascii': ('enforce all outputs to be ascii 7bits', False, bool)})
class d_corrupt_bits_by_position(Disruptor):
    '''
    Corrupt bit at a specific byte.
    '''
    def setup(self, dm, user_input):
        return True


    def disrupt_data(self, dm, target, prev_data):
        if prev_data.node:
            val = prev_data.node.to_bytes()
        else:
            val = prev_data.to_bytes()

        prev_data.add_info('corrupted bit index: %d' % self.idx)

        new_value = self.new_val if self.new_val is not None \
                    else corrupt_bits(val[self.idx-1:self.idx], n=1, ascii=self.ascii)
        msg = val[:self.idx-1]+new_value+val[self.idx:]

        prev_data.update_from_str_or_bytes(msg)

        return prev_data


@disruptor(tactics, dtype="FIX", weight=4,
           args={'path': ('graph path regexp to select nodes on which ' \
                          'the disruptor should apply', None, str),
                 'clone_node': ('if True the dmaker will always return a copy ' \
                                'of the node. (for stateless diruptors dealing with ' \
                                'big data it can be usefull to it to False)', False, bool)})
class d_fix_constraints(Disruptor):
    '''
    Fix data constraints.

    Release constraints from input data or from only a piece of it (if
    the parameter `path` is provided), then recompute them. By
    constraints we mean every generator (or function) nodes that may
    embeds constraints between nodes, and every node *existence
    conditions*.
    '''
    def setup(self, dm, user_input):
        return True

    def disrupt_data(self, dm, target, prev_data):
        if prev_data.node is None:
            prev_data.add_info('INVALID INPUT')
            return prev_data

        if self.path:
            c = NodeInternalsCriteria(mandatory_attrs=[NodeInternals.Mutable])
            l = prev_data.node.get_reachable_nodes(path_regexp=self.path,
                                                   internals_criteria=c)
            if not l:
                prev_data.add_info('INVALID INPUT')
                return prev_data

            for n in l:
                n.unfreeze(recursive=True, reevaluate_constraints=True)
                prev_data.add_info("release constraints from the node '%s'" % n.name)

        else:
            prev_data.node.unfreeze(recursive=True, reevaluate_constraints=True)
            prev_data.add_info('release constraints from the root')

        prev_data.node.freeze()

        if self.clone_node:
            exported_node = Node(prev_data.node.name, base_node=prev_data.node, new_env=True)
            prev_data.update_from_node(exported_node)

        return prev_data


@disruptor(tactics, dtype="NEXT", weight=4,
           args={'path': ('graph path regexp to select nodes on which ' \
                          'the disruptor should apply', None, str),
                 'recursive': ('apply the disruptor recursively', True, str),
                 'clone_node': ('if True the dmaker will always return a copy ' \
                                'of the node. (for stateless diruptors dealing with ' \
                                'big data it can be usefull to it to False)', False, bool)})
class d_next_node_content(Disruptor):
    '''
    Move to the next content of the nodes from input data or from only
    a piece of it (if the parameter `path` is provided). Basically,
    unfreeze the nodes then freeze them again, which will consequently
    produce a new data.
    '''
    def setup(self, dm, user_input):
        return True

    def disrupt_data(self, dm, target, prev_data):
        if prev_data.node is None:
            prev_data.add_info('INVALID INPUT')
            return prev_data

        prev_data.node.freeze()

        if self.path:
            l = prev_data.node.get_reachable_nodes(path_regexp=self.path)
            if not l:
                prev_data.add_info('INVALID INPUT')
                return prev_data

            for n in l:
                n.unfreeze(recursive=self.recursive)
                n.freeze()
                prev_data.add_info("unfreeze the node '{:s}'".format(n.get_path_from(prev_data.node)))
                prev_data.add_info("new value:        '{:s}'".format(n.to_bytes()))

        else:
            prev_data.node.unfreeze(recursive=self.recursive)
            prev_data.add_info('unfreeze from the root node')

        prev_data.node.freeze()

        if self.clone_node:
            exported_node = Node(prev_data.node.name, base_node=prev_data.node, new_env=True)
            prev_data.update_from_node(exported_node)

        return prev_data


@disruptor(tactics, dtype="MOD", weight=4,
           args={'path': ('graph path regexp to select nodes on which ' \
                          'the disruptor should apply', None, str),
                 'value': ('the new value to inject within the data', '', str),
                 'constraints': ('constraints for the absorption of the new value', AbsNoCsts(), AbsCsts),
                 'clone_node': ('if True the dmaker will always return a copy ' \
                                'of the node. (for stateless diruptors dealing with ' \
                                'big data it can be usefull to it to False)', False, bool)})
class d_modify_nodes(Disruptor):
    '''
    Change the content of the nodes specified by the regexp path with
    the value privided as a parameter (use *node absorption*
    infrastructure). If no path is provided, the root node will be
    used.

    Constraints can also be provided for absorption of the new value.
    '''
    def setup(self, dm, user_input):
        return True

    def disrupt_data(self, dm, target, prev_data):
        if prev_data.node is None:
            prev_data.add_info('INVALID INPUT')
            return prev_data

        if self.path:
            l = prev_data.node.get_reachable_nodes(path_regexp=self.path)
            if not l:
                prev_data.add_info('INVALID INPUT')
                return prev_data

            for n in l:
                status, off, size, name = n.absorb(self.value, constraints=self.constraints)
                self._add_info(prev_data, n, status, size)
        else:
            status, off, size, name = prev_data.node.absorb(self.value, constraints=self.constraints)
            self._add_info(prev_data, prev_data.node, status, size)

        prev_data.node.freeze()

        if self.clone_node:
            exported_node = Node(prev_data.node.name, base_node=prev_data.node, new_env=True)
            prev_data.update_from_node(exported_node)

        return prev_data

    def _add_info(self, prev_data, n, status, size):
        val_len = len(self.value)
        prev_data.add_info("changed node:     '{:s}'".format(n.name))
        prev_data.add_info("absorption status: {:s}".format(status))
        prev_data.add_info("value provided:   '{:s}'".format(self.value))
        prev_data.add_info("__ length:         {:d}".format(val_len))
        if status != AbsorbStatus.FullyAbsorbed:
            prev_data.add_info("absorbed size:     {:d}".format(size))
            if val_len - size > 100:
                remaining = self.value[size:size+100] + ' ...'
            else:
                remaining = self.value[size:]
            prev_data.add_info("remaining:      '{:s}'".format(remaining))


@disruptor(tactics, dtype="COPY", weight=4,
           args={})
class d_shallow_copy(Disruptor):
    '''
    Shallow copy of the input data, which means: ignore its frozen
    state during the copy.
    '''
    def setup(self, dm, user_input):
        return True

    def disrupt_data(self, dm, target, prev_data):
        if prev_data.node is None:
            prev_data.add_info('INVALID INPUT')
            return prev_data

        prev_data.add_info('shallow copy of input data has been done')
        exported_node = Node(prev_data.node.name, base_node=prev_data.node, new_env=True,
                             ignore_frozen_state=True)
        prev_data.update_from_node(exported_node)

        return prev_data


#######################
# OBSOLETE DISRUPTORS #
#######################


@disruptor(tactics, dtype="tTERM", weight=1,
           gen_args = GENERIC_ARGS,
           args={'ascii': ('enforce all outputs to be ascii 7bits', False, bool),
                 'determinist': ('make the disruptor determinist', True, bool),
                 'alt_values': ('list of alternative values to be tested ' \
                                '(replace the current base list used by the disruptor)', None, list)})
class sd_fuzz_terminal_nodes(StatefulDisruptor):
    '''
    [OBSOLETE] Perform alterations on terminal nodes (one at a time),
    without considering its type.
    '''
    def setup(self, dm, user_input):
        return True

    def set_seed(self, prev_data):
        if prev_data.node is None:
            prev_data.add_info('DONT_PROCESS_THIS_KIND_OF_DATA')
            return prev_data

        prev_data.node.make_finite(all_conf=True, recursive=True)

        self.consumer = TermNodeDisruption(max_runs_per_node=self.max_runs_per_node,
                                           min_runs_per_node=self.min_runs_per_node,
                                           respect_order=False,
                                           base_list=self.alt_values)
        self.consumer.determinist = self.determinist
        if self.ascii:
            self.consumer.ascii = True
        
        self.walker = iter(ModelWalker(prev_data.node, self.consumer, max_steps=self.max_steps, initial_step=self.init))
        
        self.max_runs = None
        self.current_node = None
        self.run_num = None

    def disrupt_data(self, dm, target, data):
        try:
            rnode, consumed_node, orig_node_val, idx = next(self.walker)
        except StopIteration:
            data.make_unusable()
            self.handover()
            return data

        new_max_runs = self.consumer.max_nb_runs_for(consumed_node)
        if self.max_runs != new_max_runs or self.current_node != consumed_node:
            self.current_node = consumed_node
            self.max_runs = new_max_runs
            self.run_num = 1
        else:
            self.run_num +=1

        data.add_info('model walking index: {:d}'.format(idx))
        data.add_info(' |_ run: {:d} / {:d} (max)'.format(self.run_num, self.max_runs))
        data.add_info('current fuzzed node: %s' % consumed_node.get_path_from(rnode))
        data.add_info('original val: %s' % repr(orig_node_val))
        data.add_info('corrupted val: %s' % repr(consumed_node.to_bytes()))

        if self.clone_node:
            exported_node = Node(rnode.name, base_node=rnode, new_env=True)
            data.update_from_node(exported_node)
        else:
            data.update_from_node(rnode)

        return data

