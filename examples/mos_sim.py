"""
# Mos DC Operating Point Simulation 

A simple, common spice-class simulation example using the built-in `sample` PDK.
"""

import hdl21 as h
import hdl21.sim as hs
from hdl21.prefix import m
import vlsirtools.spice as vsp

# Import the built-in sample PDK
from hdl21.pdk import sample_pdk


@hs.sim
class MosDcopSim:
    """# Mos Dc Operating Point Simulation Input"""

    @h.module
    class Tb:
        """# Basic Mos Testbench"""

        VSS = h.Port()  # The testbench interface: sole port VSS
        vdc = h.Vdc(dc=1000 * m)(n=VSS)  # A DC voltage source
        mos = sample_pdk.Nmos()(
            d=vdc.p, g=vdc.p, s=VSS, b=VSS
        )  # The transistor under test

    # Simulation Stimulus
    op = hs.Op()
    mod = hs.Include(sample_pdk.install.models)


def main():
    """# Run the `MosDcopSim` simulation."""

    # Set a few runtime options.
    # If you'd like a different simulator, this and the check below are the place to specify it!
    opts = vsp.SimOptions(
        simulator=vsp.SupportedSimulators.NGSPICE,
        fmt=vsp.ResultFormat.SIM_DATA,  # Get Python-native result types
        rundir="./scratch",  # Set the working directory for the simulation. Uses a temporary directory by default.
    )
    if not vsp.ngspice.available():
        print("ngspice is not available. Skipping simulation.")
        return

    # Run the simulation!
    results = MosDcopSim.run(opts)

    # Get the transistor drain current
    id = abs(results["op"].data["i(v.xtop.vvdc)"])

    # Check that it's in the expected range
    # (There's nothing magic about these numbers; they're just past sim results.)
    assert id > 115e-6
    assert id < 117e-6


if __name__ == "__main__":
    main()
