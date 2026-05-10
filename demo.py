import sys
import time

try:
    from etabs_full import main_loop
except ImportError:
    print("Error: Could not find etabs_full.py. Make sure it is in the same directory.")
    sys.exit(1)

def demo_log_callback(msg):
    """Custom log callback for a cleaner console output during the demo."""
    print(f"[DEMO] {msg}")

def main():
    print("="*60)
    print(" ETABS GENETIC ALGORITHM OPTIMIZER - DEMO MODE ")
    print("="*60)
    print("This demo will run a highly constrained version of the GA optimizer")
    print("to showcase the automated structural design workflow.")
    print("\nSettings:")
    print(" - Generations: 2 (Usually 50+)")
    print(" - Population: 2 (Usually 20+)")
    print(" - Mutations and Crossovers are enabled")
    print("="*60)
    print("Starting ETABS API connection...\n")
    
    time.sleep(2)
    
    # Overwrite default CONFIG parameters for a quick demo
    demo_params = {
        "GENS": 2,          # Very fast
        "POP": 2,           # Very small population
        "WORK_ON_COPY": True, # Don't mess up the original base model
    }
    
    try:
        # Run the main optimization loop with the custom parameters
        main_loop(gui_params=demo_params, log_callback=demo_log_callback)
        print("\n" + "="*60)
        print(" DEMO COMPLETED SUCCESSFULLY ")
        print("="*60)
        print("To see a full optimization run, modify the CONFIG in etabs_full.py")
        print("and increase GENS and POP to appropriate sizes.")
    except Exception as e:
        print(f"\n[DEMO ERROR] An error occurred during the demo: {e}")

if __name__ == "__main__":
    main()
