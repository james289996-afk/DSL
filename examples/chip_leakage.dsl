# Example: chip leakage test flow
# Run with mock mode:
#   python chiptest_dsl.py examples/chip_leakage.dsl --mock --dump-vars

CONNECT psu "TCPIP0::192.168.1.10::inst0::INSTR"
CONNECT dmm "USB0::0x2A8D::0x1301::MY12345678::INSTR"

WRITE psu "*RST"
WRITE psu "OUTP OFF"

LET vdd = 1.8
WRITE psu "VOLT {vdd}"
WRITE psu "CURR 0.01"
WRITE psu "OUTP ON"
WAIT 0.1

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

WRITE psu "OUTP OFF"
