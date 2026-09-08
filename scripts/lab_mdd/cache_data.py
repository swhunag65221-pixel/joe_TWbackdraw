import pathlib as _pl
_LAB = _pl.Path(__file__).resolve().parent
_ROOT = _LAB.parent.parent
import pickle, sys
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / 'scripts'))
from tx_data import load_tx, login
login()
bars, fs, miss = load_tx()
with open(str(_LAB / 'tx.pkl'),'wb') as f:
    pickle.dump((bars, fs, miss), f)
print(len(bars), bars[0].d, bars[-1].d, "missing rolls:", len(miss))
