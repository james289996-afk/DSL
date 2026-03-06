# Example: batch smoke test for multiple DUTs
# Run:
#   python3 chiptest_dsl.py examples/multi_dut_batch.dsl --mock --dump-vars

CONNECT psu "TCPIP0::192.168.1.10::inst0::INSTR"
CONNECT dmm "USB0::0x2A8D::0x1301::MY12345678::INSTR"

WRITE psu "*RST"
WRITE psu "OUTP OFF"
LET vdd = 1.8
WRITE psu "VOLT {vdd}"
WRITE psu "CURR 0.02"
WRITE psu "OUTP ON"

ON_FAIL
    PRINT "Batch failed, shutting down PSU"
    WRITE psu "OUTP OFF"
END

LET batch = "lot_demo_01"
LET dut_ids = ["DUT_A01", "DUT_A02", "DUT_A03"]
LET results = []
LET total_pass = 0

FOR dut IN dut_ids
    PRINT "Testing {dut} at index {loop_index}"
    RETRY 2 DELAY 0.01
        QUERY i_meas FROM dmm "MEAS:CURR:DC?"
        LET i_leak = float(i_meas)
        ASSERT i_leak < 2e-5 MESSAGE "Leakage out of retry range"
    END

    IF i_leak < 1e-6
        LET grade = "A"
    ELIF i_leak < 5e-6
        LET grade = "B"
    ELSE
        LET grade = "FAIL"
    END

    IF grade != "FAIL"
        LET total_pass = total_pass + 1
    END

    LET results = results + [{"dut": dut, "i_leak": i_leak, "grade": grade}]
END

LET yield_ratio = total_pass / len(dut_ids)
METRIC total_dut = len(dut_ids)
METRIC total_pass = total_pass
METRIC yield_ratio = yield_ratio
REPORT JSON "reports/{batch}_summary.json"
REPORT CSV "reports/{batch}_summary.csv"
PRINT "Batch {batch} finished, yield={yield_ratio}"

WRITE psu "OUTP OFF"
