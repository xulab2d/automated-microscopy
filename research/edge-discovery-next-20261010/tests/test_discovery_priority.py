import ast, importlib.util, unittest
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('priority',ROOT/'flakefinder/discovery_priority.py')
priority=importlib.util.module_from_spec(spec);spec.loader.exec_module(priority)
source=ast.parse((ROOT/'flakefinder/sparse_acquisition.py').read_text())
fn=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='_gap_probe_direction')
space={'np':np};exec(compile(ast.Module(body=[fn],type_ignores=[]),'<staged-gap>','exec'),space)
gap=space['_gap_probe_direction']
class TestDiscoveryPriority(unittest.TestCase):
 def test_missing_sector_cannot_point_toward_known_edges(self):
  lines=[{'normal':[-.981097,-.193517]},{'normal':[.1816286,.9833672]}]
  q=gap(lines,[0,0],[-9000,9000,-9000,9000]);self.assertIsNotNone(q)
  self.assertTrue(all(np.dot(q['direction'],x['normal'])<=1e-9 for x in lines))
  self.assertIsNone(gap(lines,[0,0],[-9000,9000,-9000,9000],[q['direction']]))
 def test_three_edges_missing_east(self):
  lines=[{'normal':[-1,0]},{'normal':[0,1]},{'normal':[0,-1]}]
  q=gap(lines,[0,0],[-9,9,-9,9]);np.testing.assert_allclose(q['direction'],[1,0],atol=1e-9)
 def data(self):
  return {'seed_xy_um':[0,0],'options':{'search_bounds_um':[-9,9,-9,9]},
   'rays':{'east':{'status':'unresolved','direction':[1,0],'last_substrate_distance_um':9,'first_holder_distance_um':None}},
   'records':[{'position_um':[x,-3,1],'classification':'substrate'} for x in [3,5,7]]}
 def test_reseed_reorders_only_pending_and_needs_range_hit(self):
  d=self.data();centres=[[0,0],[5,-3],[7,-3]];pending={0,1,2}
  r=priority.interior_reseed(d,centres,pending,minimum_shift_um=3)
  self.assertEqual(r['index'],1);self.assertEqual(pending,{0,1,2})
  d['rays']['east']['first_holder_distance_um']=8
  self.assertIsNone(priority.interior_reseed(d,centres,pending,minimum_shift_um=3))
 def test_no_reseed_to_consumed_or_too_near(self):
  d=self.data();centres=[[0,0],[5,-3],[7,-3]]
  self.assertEqual(priority.interior_reseed(d,centres,{0,2},minimum_shift_um=3)['index'],2)
  self.assertIsNone(priority.interior_reseed(d,centres,{0},minimum_shift_um=3))
if __name__=='__main__':unittest.main()
