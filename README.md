# ETABS Genetic Algorithm Optimizer 🧬🏢

Welcome to the **ETABS Genetic Algorithm Optimizer**! This Python-based tool automates the structural design and sizing of beams and columns in [CSI ETABS](https://www.csiamerica.com/products/etabs) using evolutionary algorithms. By intelligently exploring the design space, it minimizes total construction cost (concrete volume and rebar weight) while strictly adhering to structural design codes (like ACI 318-19).

## 🚀 What it Does

This script connects directly to an active (or new) ETABS instance via the COM API. It treats the structural framing as an evolutionary ecosystem:
1. **Initial Population:** Generates a set of random, physically plausible beam and column sizes and material grades.
2. **Analysis & Design:** Automatically assigns these properties to the ETABS model, runs the structural analysis, and executes concrete design.
3. **Fitness Evaluation:** Extracts concrete volume and reinforcement data, calculating the total cost. It applies heavy penalties for design failures or excessive drift.
4. **Evolution:** Uses Simulated Binary Crossover (SBX) and polynomial mutation to "breed" the best designs over multiple generations until it finds the optimal, most cost-effective structure.

## 📁 Repository Structure

- `etabs_full.py`: The core Genetic Algorithm optimizer and ETABS API client.
- `demo.py`: A fast, lightweight script designed to showcase the optimizer in action quickly.
- `dump_rebar_values_full.py`: A utility script to extract detailed rebar quantities and generate a CSV report.
- `Trial.EDB`: A sample ETABS model provided for testing the optimizer.
- `showcase_assets/`: Contains templates and assets for sharing this project on LinkedIn and GitHub.

## 🛠️ Installation & Setup

1. **Prerequisites:**
   - Python 3.8+
   - CSI ETABS (installed and licensed on your machine)
   - Windows OS (required for the COM API)

2. **Clone the Repository:**
   ```bash
   git clone https://github.com/EisaMehmood/etabs_optimizer.git
   cd etabs_optimizer
   ```

3. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   *(Main dependencies: `comtypes`, `pandas`, `numpy`)*

## 🏃‍♂️ Quick Start (Demo Mode)

Want to see the algorithm in action without waiting hours for a full evolution? Run the demo script! 

```bash
python demo.py
```

The demo mode uses a tiny population (2) and only runs for 2 generations. It operates on a timestamped copy of `Trial.EDB` so your original model is safe. Watch the console output as it mates, mutates, and evaluates the structural frames.

## ⚙️ Advanced Usage (Full Optimization)

To run a full-scale optimization:
1. Open `etabs_full.py` in your editor.
2. Modify the `CONFIG` dictionary at the top of the file:
   - Set `"GENS"` to `50` or higher.
   - Set `"POP"` to `20` or higher.
   - Adjust `"BASE_EDB"` to point to your specific ETABS model.
   - Fine-tune material costs (`"COST_PER_M3"`, `"REBAR_COST_PER_KG"`).
3. Run the script:
   ```bash
   python etabs_full.py
   ```

## 📜 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---
*Created by [Your Name/Handle]. Feel free to open issues or submit pull requests if you have ideas to improve the algorithm!*
