### 🎯 Key Features

- **🧠 Intelligent Evolutionary Optimization:** Implements a custom Genetic Algorithm (GA) using Simulated Binary Crossover (SBX) and polynomial mutation to efficiently search the vast design space of structural dimensions and material grades.
- **🔌 Direct ETABS Integration:** Utilizes the COM API (`comtypes`) to programmatically drive ETABS. The script automatically builds sections, assigns properties, runs analysis, and queries design results without manual intervention.
- **💰 Multi-Objective Cost Function:** The fitness function minimizes total construction cost by calculating precise concrete volumes and rebar weights (via ETABS Reinforcement Data tables), applying specific unit rates for different concrete (C20-C50) and steel (S270-S550) grades.
- **🛡️ Rigorous Constraint Handling:** Ensures real-world structural viability by heavily penalizing failed members, aspect ratio violations, and excessive story drifts (H/50 limits).
- **🚀 Demo Mode Included:** Features a fast-execution `demo.py` script that allows users to quickly test the optimization loop on a small population, perfect for learning and demonstration.
- **📊 Comprehensive Data Export:** Includes a secondary script (`dump_rebar_values_full.py`) that extracts and tabulates detailed rebar data for every frame element into CSV format for further analysis.
