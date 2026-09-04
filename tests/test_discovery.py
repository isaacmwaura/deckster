"""mDNS advertiser: service info + register/unregister lifecycle (no real network)."""
from agent.discovery import SERVICE_TYPE, Advertiser


class FakeZC:
    def __init__(self):
        self.registered = []
        self.unregistered = []
        self.closed = False

    def register_service(self, info):
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)

    def close(self):
        self.closed = True


def test_make_info_carries_port_and_secure_flag():
    info = Advertiser(9000, secure=True)._make_info()
    assert info.type == SERVICE_TYPE
    assert info.port == 9000
    assert info.properties[b"secure"] == b"1"


def test_start_then_stop_registers_and_unregisters():
    zc = FakeZC()
    adv = Advertiser(8765, zc_factory=lambda: zc)
    adv.start()
    assert len(zc.registered) == 1
    adv.stop()
    assert len(zc.unregistered) == 1 and zc.closed


def test_set_secure_reregisters_when_running():
    made = []

    def factory():
        zc = FakeZC()
        made.append(zc)
        return zc

    adv = Advertiser(8765, secure=False, zc_factory=factory)
    adv.start()
    adv.set_secure(True)                    # stop old, start fresh with new TXT
    assert made[0].closed
    assert len(made) == 2
    assert made[1].registered[0].properties[b"secure"] == b"1"
    adv.stop()
