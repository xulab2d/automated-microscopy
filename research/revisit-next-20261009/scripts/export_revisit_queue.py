"""Create an offline, source-verified review queue; never controls hardware."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flakefinder.revisit_queue import export_graphene_revisit_queue

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--candidate-id', action='append', help='Repeat to explicitly include retained uncertain observations.')
    parser.add_argument('--max-candidates', type=int, default=25)
    parser.add_argument('--profile', type=Path, help='Optional measured100x profile; this does not authorize motion.')
    args = parser.parse_args()
    result = export_graphene_revisit_queue(args.project, args.run_id, args.output, candidate_ids=args.candidate_id,
                                          max_candidates=args.max_candidates, profile_path=args.profile)
    print(json.dumps({'candidate_count': result['candidate_count'], 'readiness': result['readiness'],
                      'hardware_accessed': False}, indent=2))
