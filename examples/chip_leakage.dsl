# Example: chip leakage test flow
# Run with mock mode:
#   python3 chiptest_dsl.py examples/chip_leakage.dsl --mock --dump-vars

CONNECT psu "TCPIP0::192.168.1.10::inst0::INSTR"
CONNECT dmm "USB0::0x2A8D::0x1301::MY12345678::INSTR"

WRITE psu "*RST"
WRITE psu "OUTP OFF"

ON_FAIL
    PRINT "Test failed, forcing PSU output OFF"
    WRITE psu "OUTP OFF"
END

LET vdd = 1.8
WRITE psu "VOLT {vdd}"
WRITE psu "CURR 0.01"
LET boot_try = 0
RETRY 3 DELAY 0.05
    LET boot_try = boot_try + 1
    WRITE psu "OUTP ON"
    WAIT 0.05
    # Simulate first-attempt failure to demonstrate RETRY
    ASSERT boot_try >= 2 MESSAGE "PSU output not stable yet"
END

QUERY idn FROM dmm "*IDN?"
PRINT "DMM ID: {idn}"

LET samples = []
REPEAT 5
    QUERY i_meas FROM dmm "MEAS:CURR:DC?"
    LET samples = samples + [float(i_meas)]
    WAIT 0.02
END

LET avg_i = sum(samples) / len(samples)
PRINT "Average leakage current = {avg_i} A"
ASSERT avg_i < 1e-5 MESSAGE "Leakage current too high"

IF avg_i < 1e-6
    LET grade = "A"
ELIF avg_i < 5e-6
    LET grade = "B"
ELSE
    LET grade = "C"
END

METRIC vdd = vdd
METRIC avg_leakage = avg_i
METRIC grade = grade
METRIC retry_used = retry_attempt
REPORT JSON "reports/chip_leakage.json"
REPORT CSV "reports/chip_leakage.csv"
PRINT "Test grade: {grade}, retry attempt used: {retry_attempt}"

WRITE psu "OUTP OFF"
