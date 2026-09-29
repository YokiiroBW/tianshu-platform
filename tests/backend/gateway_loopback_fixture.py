"""Recorded TLS model target for the isolated joint test only."""

from tianshu_gateway.__main__ import main
from tianshu_gateway.provider_adapter import TargetPolicy

original = TargetPolicy.permits


def synthetic_target(self, address, connection_type):
    return (address == "127.0.0.1" and connection_type == "public") or original(
        self, address, connection_type
    )


TargetPolicy.permits = synthetic_target
main()
