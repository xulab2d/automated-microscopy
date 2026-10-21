"""Load review copies under a separate package; never monkeypatch live imports."""
from pathlib import Path
import sys, types

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path.insert(0,str(ROOT))
package=types.ModuleType('review_ff')
package.__path__=[str(HERE/'flakefinder'),str(ROOT/'flakefinder')]
sys.modules['review_ff']=package


def profile_for(frame,profile_id='offline-representative-manual20x-dark-holder'):
    from review_ff.layout import _context
    return {'schema':1,'profile_id':profile_id,'holder_model':'dark_holder_all_rgb_v1',
        'manual_objective':True,'objective_position':2,'objective_magnification':20,
        'optical_context':_context(frame)}
