# Physics-Aware KiCad Router

A specialized KiCad autorouter that integrates physical simulations (Ngspice, OpenEMS) to validate designs during the routing process.

## Overview
This tool aims to produce production-ready PCB layouts by taking into account complex physical phenomena that standard routers ignore:
- **EMI Emissions**: Uses OpenEMS to simulate and minimize electromagnetic interference.
- **Power Loops**: Optimizes routing to minimize power loops and their associated parasitic inductances.
- **Signal Integrity**: Validates traces and impedance matching.
- **Ngspice Integration**: Performs circuit-level simulations to ensure design constraints are met.

## Features
- **KiCad Integration**: Works seamlessly with KiCad schematics and PCB layouts.
- **Automated Validation**: Runs iterative checks to refine the routing based on physical simulation feedback.
- **Production-Ready**: Focuses on minimizing issues in manufacturing and real-world operation.

## Setup Requirements (Planned)
- KiCad 8+
- Ngspice
- OpenEMS
- Python 3.10+
