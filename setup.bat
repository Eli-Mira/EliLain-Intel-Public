@echo off
echo === Installing EliLain-Intel-Public ===
python -m venv .venv
.venv\Scripts\activate
pip install -e .
echo Done. Run: python main.py
