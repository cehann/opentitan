# Copyright lowRISC contributors (OpenTitan project).
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

from enum import IntEnum
from typing import Dict, List, Optional

from shared.mem_layout import get_memory_layout

from .csr import CSRFile
from .dmem import Dmem
from .constants import ErrBits, LcTx, Status
from .edn_client import EdnClient
from .ext_regs import OTBNExtRegs
from .flags import FlagReg
from .gpr import GPRs
from .loop import LoopStack
from .reg import RegFile
from .trace import Trace, TracePC
from .wsr import WSRFile

# The number of cycles spent per round of a secure wipe. This takes constant
# time in the RTL, mirrored here.
_WIPE_CYCLES = 68


class FsmState(IntEnum):
    r'''State of the internal start/stop FSM

    The FSM diagram looks like:

        MEM_SEC_WIPE <--\
             |          |
             \-------> IDLE -> PRE_EXEC -> EXEC
                         ^                   |
                         |                   v
                         \----WIPING <-> PRE_WIPE
                                 |
                 LOCKED <--------/

    PRE_WIPE is the initial state. OTBN is requesting a URND seed from the EDN.
    Once it arrives, we jump to WIPING and perform an actual round of wiping
    internal state. Once that wipe is finished, we normally jump back to
    PRE_WIPE and then back to WIPING again.

    Once WIPING has finished for the second time, we jump to LOCKED if there
    has been an RMA request or an error. If not, we jump to IDLE.

    IDLE represents the state when nothing is going on but there have been no
    fatal errors. It matches Status.IDLE. LOCKED represents the state when
    there has been a fatal error. It matches Status.LOCKED.

    MEM_SEC_WIPE only represents the state where OTBN is busy operating on
    secure wipe of DMEM/IMEM to perform the SEC_WIPE_I(D)MEM command. Secure
    wipe of the memories also happen when we encounter a fatal error while on
    Status.BUSY_EXECUTE. However, if we are getting a fatal error Status would
    be LOCKED.

    PRE_EXEC is the period after starting OTBN where we're still waiting for an
    EDN value to seed URND. EXEC is the period where we start fetching and
    executing instructions.
    '''
    PRE_WIPE = 0
    WIPING = 1
    IDLE = 2
    PRE_EXEC = 3
    EXEC = 4
    MEM_SEC_WIPE = 10
    LOCKED = 255


class InitSecWipeState(IntEnum):
    NOT_DONE = 0
    IN_PROGRESS = 1
    DONE = 2


class OTBNState:
    def __init__(self) -> None:
        self.gprs = GPRs()
        self.wdrs = RegFile('w', 256, 32)

        self.ext_regs = OTBNExtRegs()
        self.wsrs = WSRFile(self.ext_regs)
        self.csrs = CSRFile()

        self.pc = 0
        self._pc_next_override: Optional[int] = None

        self.imem_size = get_memory_layout().imem_size_bytes

        self.dmem = Dmem()

        self._fsm_state = FsmState.PRE_WIPE
        self._next_fsm_state = FsmState.PRE_WIPE

        self._init_sec_wipe_state = InitSecWipeState.NOT_DONE

        # Track how many rounds of secure wipe to do. This is normally 2, but
        # we shorten things to a single round when we get an RMA req, which
        # avoids needing to wait for URND data from an entropy complex that
        # might not be available.
        self.wipe_rounds_to_do = 2

        # Track the number of rounds of secure wipe that have been done. This
        # should never be more than wipe_rounds_to_do.
        self.wipe_rounds_done = 0

        self.loop_stack = LoopStack()

        self._err_bits = 0
        self.pending_halt = False

        self._urnd_client = EdnClient()

        # To simulate injecting integrity errors, we set a flag to say that
        # IMEM is no longer readable without getting an error. This can't take
        # effect instantly because the RTL's prefetch stage (which we don't
        # model except for matching its timing) has a copy of the next
        # instruction. Make this a counter, decremented once per cycle. When we
        # get to zero, we set the flag.
        self._time_to_imem_invalidation: Optional[int] = None
        self.invalidated_imem = False

        # This is the number of cycles left for wiping. When we're in the
        # WIPING state, this should be a non-negative number. Initialise to -1
        # to catch bugs if we forget to set it.
        self.wipe_cycles = -1

        # This controls which state we move to when the next secure wipe ends
        self.lock_after_wipe = False

        # If this is nonzero, there's been an error injected from outside. The
        # Sim.step function will OR these bits into err_bits and stop at the
        # end of the next cycle. (We don't just call stop_at_end_of_cycle
        # because this runs earlier in the cycle than step(), which would mean
        # we wouldn't see the final instruction get executed and then
        # cancelled).
        self.injected_err_bits = 0
        self.lock_immediately = False

        # OTBN might zero its insn_cnt register during a secure wipe. The
        # precise cycle that this happens depends slightly on how we decide to
        # do so. If this is not None, it is a counter of the number of cycles
        # before the zeroing should happen.
        self.time_to_insn_cnt_zero: Optional[int] = None

        # If this is set, all software errors should result in the model status
        # being locked.
        self.software_errs_fatal = False

        # This is a counter that keeps track of how many cycles have elapsed in
        # current fsm_state.
        self.cycles_in_this_state = 0

        # An RMA request is seen if the rma_req_i signal (tracked as rma_req
        # here) is ON at a particular time.
        #
        # The signal is observed by OTBN at a few specific times:
        #
        #   - When OTBN is idle (the 'initial' and 'halt' states in
        #     otbn_start_stop_control)
        #
        #   - At the end of a secure wipe (the 'wipe complete' state in
        #     otbn_start_stop_control). It gets sampled at that particular
        #     point to allow an RMA to be chosen after triggering an error but
        #     before the secure wipe is completed and the module locks
        #     completely.
        #
        # When the signal is observed to be LcTx.ON, the response is to change
        # state to LOCKED (a bit like an escalation from lifecycle controller).
        self.rma_req = LcTx.OFF

        # This flag gets set as soon as we leave the Idle state for the first
        # time. It reflects the behaviour of wipe_after_urnd_refresh_q in
        # otbn_start_stop_control.sv, which skips a round of secure wiping
        self.has_state_to_wipe = False

        # If this flag is set, jump straight to the LOCKED state when we step
        # in IDLE.
        self.delayed_lock = False

        # We set this flag when the first URND seed comes back from the EDN. If
        # we get an RMA request before this flag is set, we'll be in the
        # PRE_WIPE state and the EDN might not actually be running. In that
        # situation, we do a shortened secure wipe (just one round and no
        # random data).
        self.edn_seen_running = False

    def get_next_pc(self) -> int:
        if self._pc_next_override is not None:
            return self._pc_next_override
        return self.pc + 4

    def set_next_pc(self, next_pc: int) -> None:
        '''Overwrite the next program counter, e.g. as result of a jump.'''
        assert (self.is_pc_valid(next_pc))
        self._pc_next_override = next_pc

    def edn_urnd_step(self, urnd_data: int) -> None:
        self._urnd_client.take_word(urnd_data, False)

    def edn_rnd_step(self, rnd_data: int, fips_err: bool) -> None:
        self.ext_regs.rnd_take_word(rnd_data, fips_err)

    def edn_flush(self) -> None:
        self.ext_regs.rnd_reset()
        self._urnd_client.edn_reset()
        # If the initial secure wipe is running, OTBN will directly request a
        # new URND value.
        if self.init_sec_wipe_is_running():
            self._urnd_client.request()

    def rnd_completed(self) -> None:
        '''Called when CDC completes for the EDN RND interface'''
        # Set the RND WSR with the value, assuming the cache hadn't been
        # poisoned. This will be committed at the end of the next step on the
        # main clock.
        rnd_val, fips_err, rep_err = self.ext_regs.rnd_cdc_complete()
        if rnd_val is not None:
            self.wsrs.RND.set_unsigned(rnd_val, fips_err, rep_err)

    def urnd_completed(self) -> None:
        w256, retry, _, _ = self._urnd_client.cdc_complete()
        # The URND client should never be poisoned
        assert w256 is not None and retry is False

        # cdc_complete() returned a 256-bit value but we actually need to split
        # it back into four 64-bit words.
        w64s = [(w256 >> (64 * i)) & ((1 << 64) - 1) for i in range(4)]

        self.edn_seen_running = True

        self.wsrs.URND.set_seed(w64s)

    def start_init_sec_wipe(self) -> None:
        self._init_sec_wipe_state = InitSecWipeState.IN_PROGRESS
        # OTBN will request a new URND value, so the model has to do the same.
        self._urnd_client.request()

    def init_sec_wipe_is_running(self) -> bool:
        return self._init_sec_wipe_state == InitSecWipeState.IN_PROGRESS

    def init_sec_wipe_is_done(self) -> bool:
        return self._init_sec_wipe_state == InitSecWipeState.DONE

    def complete_init_sec_wipe(self) -> None:
        self._init_sec_wipe_state = InitSecWipeState.DONE

    def loop_start(self, iterations: int, bodysize: int) -> None:
        self.loop_stack.start_loop(self.pc + 4, iterations, bodysize)

    def loop_step(self, loop_warps: Dict[int, int]) -> None:
        back_pc = self.loop_stack.step(self.pc, loop_warps)
        if back_pc is not None:
            self.set_next_pc(back_pc)

    def in_loop(self) -> bool:
        '''The processor is currently executing a loop.'''

        # A loop is executed if the loop stack is not empty.
        return bool(self.loop_stack.stack)

    def changes(self) -> List[Trace]:
        c: List[Trace] = []
        c += self.gprs.changes()
        if self._pc_next_override is not None:
            # Only append the next program counter to the trace if it has
            # been set explicitly.
            c.append(TracePC(self.get_next_pc()))
        c += self.dmem.changes()
        c += self.loop_stack.changes()
        c += self.ext_regs.changes()
        c += self.wsrs.changes()
        c += self.csrs.flags.changes()
        c += self.wdrs.changes()
        return c

    def executing(self) -> bool:
        return self._fsm_state not in [FsmState.IDLE,
                                       FsmState.LOCKED,
                                       FsmState.MEM_SEC_WIPE]

    def wiping(self) -> bool:
        return self._fsm_state == FsmState.WIPING

    def stop_if_pending_halt(self) -> bool:
        if self.pending_halt:
            self.stop()
            return True
        return False

    def step(self, handle_injected_error: bool) -> None:
        if handle_injected_error:
            self.take_injected_err_bits()
        self.ext_regs.step()
        self._urnd_client.step()

    def commit(self, sim_stalled: bool) -> None:
        if self._time_to_imem_invalidation is not None:
            self._time_to_imem_invalidation -= 1
            if self._time_to_imem_invalidation == 0:
                self.invalidated_imem = True
                self._time_to_imem_invalidation = None

        old_state = self._fsm_state

        self._fsm_state = self._next_fsm_state

        if self._fsm_state == old_state:
            self.cycles_in_this_state += 1
        else:
            self.cycles_in_this_state = 0

        self.ext_regs.commit()

        # Pull URND out separately because we also want to commit this in some
        # "idle-ish" states
        self.wsrs.URND.commit()

        # In some states, we can get away with just committing external
        # registers (which lets us reflect things like the update to the STATUS
        # register) but nothing else. This is just an optimisation: if
        # everything is working properly, there won't be any other pending
        # changes.
        if old_state not in [FsmState.EXEC, FsmState.WIPING]:
            return

        self.gprs.commit()
        self.dmem.commit()
        self.loop_stack.commit()
        self.wsrs.commit()
        self.csrs.flags.commit()
        self.wdrs.commit()

        if not sim_stalled:
            self.pc = self.get_next_pc()
            self._pc_next_override = None

    def _abort(self) -> None:
        '''Abort any pending state changes'''
        self.gprs.abort()
        self._pc_next_override = None
        self.dmem.abort()
        self.loop_stack.abort()
        self.ext_regs.abort()
        self.wsrs.abort()
        self.csrs.flags.abort()
        self.wdrs.abort()

    def start(self) -> None:
        '''Start running; perform state init'''
        self.ext_regs.write('STATUS', Status.BUSY_EXECUTE, True)
        self.pending_halt = False
        self._err_bits = 0

        self._fsm_state = FsmState.PRE_EXEC
        self._next_fsm_state = FsmState.PRE_EXEC
        self.has_state_to_wipe = True

        self.pc = 0

        # Reset CSRs, WSRs, loop stack and call stack. WSRs have special
        # treatment because some of them have values that persist across
        # operations.
        self.csrs = CSRFile()
        self.wsrs.on_start()
        self.loop_stack = LoopStack()
        self.gprs.empty_call_stack()

        # Poison the requester so that we'll discard the rest of any in-flight
        # request.
        self.ext_regs.rnd_poison()

        self._urnd_client.request()

    def stop(self) -> None:
        '''Set flags to stop the processor and maybe abort the instruction.

        If the current instruction has caused an error (so self._err_bits is
        nonzero), abort all its pending changes, including changes to external
        registers.

        If not, we've just executed an ECALL. The only pending change will be
        the increment of INSN_CNT that we want to keep.

        Either way, set the appropriate bits in the external ERR_CODE register,
        write STOP_PC and start a secure wipe.

        It's possible that we are already doing a secure wipe. For example, we
        might have had an error escalation signal after starting the wipe. In
        this case, there's nothing to do except possibly to update ERR_BITS.
        '''

        # If we were running an instruction and something went wrong then it
        # might have updated state (either registers, memory or
        # externally-visible registers). We want to roll back any of those
        # changes.
        insn_failed = self._err_bits and self._fsm_state == FsmState.EXEC
        if insn_failed:
            self._abort()

        # INTR_STATE is the interrupt state register. Bit 0 (which is being
        # set) is the 'done' flag.
        self.ext_regs.set_bits('INTR_STATE', 1 << 0)

        should_lock = (((self._err_bits >> 16) != 0) or
                       ((self._err_bits >> 10) & 1 != 0) or
                       (self._err_bits != 0 and self.software_errs_fatal) or
                       self.rma_req == LcTx.ON)
        # Make any error bits visible
        self.ext_regs.write('ERR_BITS', self._err_bits, True)

        # Clear the "we should stop soon" flag
        self.pending_halt = False

        if self.lock_immediately:
            assert should_lock
            self.set_fsm_state(FsmState.LOCKED)
            self.ext_regs.write('STATUS', Status.LOCKED, True)
        else:
            # Set the WIPE_START flag if we were running. This is used to tell
            # the C++ model code that this is a good time to inspect DMEM and
            # check that the RTL and model match. The flag will be cleared
            # again on the next cycle.
            if self._fsm_state == FsmState.EXEC:
                # Make the final PC visible. This isn't currently in the RTL,
                # but is useful in simulations that want to track whether we
                # stopped where we expected to stop.
                self.ext_regs.write('STOP_PC', self.pc, True)

                self.ext_regs.write('WIPE_START', 1, True)
                self.ext_regs.regs['WIPE_START'].commit()

                # Switch to the pre-wipe state
                self.set_fsm_state(FsmState.PRE_WIPE)
                self.lock_after_wipe = should_lock
                self.wipe_rounds_done = 0
            elif self._fsm_state in [FsmState.PRE_WIPE, FsmState.WIPING]:
                assert should_lock
                self.lock_after_wipe = True
            elif self._init_sec_wipe_state in [InitSecWipeState.IN_PROGRESS]:
                # Make it so that we run stop method until initial secure wipe
                # is done. Otherwise we would have missed the pending halt.
                assert should_lock
                self.pending_halt = True
            elif self._init_sec_wipe_state == InitSecWipeState.DONE:
                assert should_lock
                self._next_fsm_state = FsmState.LOCKED
                next_status = Status.LOCKED
                self.ext_regs.write('STATUS', next_status, True)

        # Clear any pending request in the RND EDN client
        self.ext_regs.rnd_forget()

    def get_fsm_state(self) -> FsmState:
        return self._fsm_state

    def set_fsm_state(self, new_state: FsmState) -> None:
        # If we're switching to a wiping state, we consider this to be "the
        # start of a wipe" and set the wipe_cycles counter to track how long
        # the wiping operation itself will take.
        wiping_next = new_state == FsmState.WIPING
        if wiping_next:
            self.wipe_cycles = _WIPE_CYCLES
        self._next_fsm_state = new_state

    def set_flags(self, fg: int, flags: FlagReg) -> None:
        '''Update flags for a flag group'''
        self.csrs.flags[fg] = flags

    def set_mlz_flags(self, fg: int, result: int) -> None:
        '''Update M, L, Z flags for a flag group using the given result'''
        self.csrs.flags[fg] = \
            FlagReg.mlz_for_result(self.csrs.flags[fg].C, result)

    def pre_insn(self, insn_affects_control: bool) -> None:
        '''Run before running an instruction'''
        self.loop_stack.check_insn(self.pc, insn_affects_control)

    def is_pc_valid(self, pc: int) -> bool:
        '''Return whether pc is a valid program counter.'''
        # The PC should always be non-negative since it's represented as an
        # unsigned value. (It's an error in the simulator if that's come
        # unstuck)
        assert 0 <= pc

        # Check the new PC is word-aligned
        if pc & 3:
            return False

        # Check the new PC lies in instruction memory
        if pc >= self.imem_size:
            return False

        return True

    def post_insn(self, loop_warps: Dict[int, int]) -> None:
        '''Update state after running an instruction but before commit'''
        self.ext_regs.increment_insn_cnt()
        self.loop_step(loop_warps)
        self.gprs.post_insn()

        self._err_bits |= self.gprs.err_bits() | self.loop_stack.err_bits()
        if self._err_bits:
            self.pending_halt = True

        # Check that the next PC is valid, but only if we're not stopping
        # anyway. This handles the case where we have a straight-line
        # instruction at the top of memory. Jumps and branches to invalid
        # addresses are handled in the instruction definition.
        #
        # This check is squashed if we're already halting, which avoids a
        # problem when you have an ECALL instruction at the top of memory (the
        # next address is bogus, but we don't care because we're stopping
        # anyway).
        if not self.is_pc_valid(self.get_next_pc()) and not self.pending_halt:
            self._err_bits |= ErrBits.BAD_INSN_ADDR
            self.pending_halt = True

    def read_csr(self, idx: int) -> int:
        '''Read the CSR with index idx as an unsigned 32-bit number'''
        return self.csrs.read_unsigned(self.wsrs, idx)

    def write_csr(self, idx: int, value: int) -> None:
        '''Write value (an unsigned 32-bit number) to the CSR with index idx'''
        self.csrs.write_unsigned(self.wsrs, idx, value)

    def peek_call_stack(self) -> List[int]:
        '''Return the current call stack, bottom-first'''
        return self.gprs.peek_call_stack()

    def stop_at_end_of_cycle(self, err_bits: int) -> None:
        '''Tell the simulation to stop at the end of the cycle

        Any bits set in err_bits will be set in the ERR_BITS register when
        we're done.

        '''
        self._err_bits |= err_bits
        self.pending_halt = True

    def invalidate_imem(self) -> None:
        self._time_to_imem_invalidation = 2

    def clear_imem_invalidation(self) -> None:
        '''Clear any effective or pending IMEM invalidation'''
        self._time_to_imem_invalidation = None
        self.invalidated_imem = False

    def wipe(self) -> None:
        self.gprs.wipe()
        self.wdrs.wipe()
        self.wsrs.wipe()
        self.csrs.wipe()

    def take_injected_err_bits(self) -> None:
        '''Apply any injected errors, stopping at the end of the cycle'''
        if self.injected_err_bits != 0:
            self.stop_at_end_of_cycle(self.injected_err_bits)
            self.injected_err_bits = 0
