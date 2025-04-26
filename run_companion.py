#!/usr/bin/env python3
# run_companion.py
# Script to start the Akita eMail Companion CLI.

import sys
import os

# --- Dynamic Path Setup ---
# Add the parent directory (project root) to the Python path
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# --- Import and Run ---
try:
    # Import the main function from the companion CLI module
    from akita_email.companion_cli import run_companion
except ImportError as e:
    print(f"Error importing Akita eMail companion module: {e}", file=sys.stderr)
    print("Please ensure the script is run from the project root directory", file=sys.stderr)
    print("or that the 'akita_email' package is installed correctly.", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"An unexpected error occurred during import: {e}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    # Execute the main function from the companion_cli module
    run_companion()
