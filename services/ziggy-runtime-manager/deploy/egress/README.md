# Egress Gateway Contract

`policy.contract.json` is a fail-closed contract for the trusted dual-homed
egress gateway. It is not a gateway implementation and cannot be mounted into
the Nanobot runtime. The selected gateway must translate every field into an
enforcing configuration, expose the labels tested by `../verify-isolation.sh`,
and reject startup when it cannot load the policy.

The runtime manager creates an internal-only tenant network. It never connects
the runtime to an uplink network. Therefore an absent or unhealthy egress
gateway breaks model/MCP access instead of granting raw host, LAN, or Internet
egress. Host firewall policy must additionally deny any rootless-network
bypass, including IPv6 and UDP.
