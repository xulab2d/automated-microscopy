"""Explicit simulator optical contract; never supplies real hardware settings."""
from test_chip_discovery import ImageStage as BaseStage
from flakefinder.layout import _context
from flakefinder.chip_discovery import file_hash
from flakefinder.photometric_edges import ALGORITHM_VERSION
import flakefinder.sparse_edges as edges
import flakefinder.photometric_edges as photo

class ImageStage(BaseStage):
    def capture(self):
        frame=super().capture()
        frame.metadata.update(objective={'magnification':20},camera_job_template='synthetic fixed20x',illumination_percent=90)
        return frame

def profile_for(frame):
    return {'schema':1,'profile_id':'synthetic20x-dark-holder','holder_model':'dark_holder_all_rgb_v1',
        'manual_objective':True,'objective_position':2,'objective_magnification':20,'optical_context':_context(frame)}

def campaign_contract(frame):
    return {'algorithm':ALGORITHM_VERSION,'profile':profile_for(frame),
        'edge_model_sha256':file_hash(edges.__file__),'photometric_model_sha256':file_hash(photo.__file__)}

def discovery_contract(frame):
    result=campaign_contract(frame);result['edge_validation_profile']=result.pop('profile');return result
