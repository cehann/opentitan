# Copyright lowRISC contributors (OpenTitan project).
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

# This line is a default case statement which could be executed if a parasitic state has been
# injected to the state register of u_prim_alert_sender.
check_cov -waiver -add -start_line 249 -end_line 249 -type {branch} -instance {dut.gen_alert_tx[0].u_prim_alert_sender} -comment {No parasitic state injection while doing FPV}

# The intention to add this assertion is to make sure that while doing FPV there is not a
# possibility to inject parasitic state to the state register of alert sender FSM.
assert -name AlertSenderNoParasiticState_A {dut.gen_alert_tx[0].u_prim_alert_sender.state_q <= 6}

# The port data_o in u_data_chk is untied and used nowhere.
check_cov -waiver -add -start_line 25 -end_line 56 -type {statement} -instance {dut.u_reg.u_chk.u_tlul_data_integ_dec.u_data_chk} -comment {data_o is untied}

# The port data_o in u_data_chk is untied and used nowhere.
check_cov -waiver -add -start_line 25 -end_line 81 -type {statement} -instance {dut.u_reg.u_chk.u_chk} -comment {data_o is untied}
