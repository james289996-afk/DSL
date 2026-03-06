# Example: demonstrate ON_FAIL callback
# Run:
#   python3 chiptest_dsl.py examples/on_fail_demo.dsl --mock

CONNECT psu "TCPIP0::192.168.1.10::inst0::INSTR"
WRITE psu "OUTP ON"

ON_FAIL
    PRINT "ON_FAIL callback executed"
    WRITE psu "OUTP OFF"
END

ASSERT 1 == 0 MESSAGE "Intentional failure for ON_FAIL demonstration"
