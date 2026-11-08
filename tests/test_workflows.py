import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "workflows"


class WorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.t2v = json.loads((WORKFLOWS / "t2v.api.json").read_text(encoding="utf-8"))
        self.i2v = json.loads((WORKFLOWS / "i2v.api.json").read_text(encoding="utf-8"))
        self.continuation = json.loads((WORKFLOWS / "continue.api.json").read_text(encoding="utf-8"))
        self.manifest = json.loads((WORKFLOWS / "manifest.json").read_text(encoding="utf-8"))

    def test_required_output_chain_exists(self):
        for graph in (self.t2v, self.i2v, self.continuation):
            types = {node["class_type"] for node in graph.values()}
            self.assertTrue({"MiniMaxH3ImageToVideo", "VAEDecode", "VAEDecodeAudio", "CreateVideo", "SaveVideo"}.issubset(types))

    def test_i2v_center_crops_before_first_frame(self):
        self.assertEqual(self.i2v["16"]["class_type"], "ImageScale")
        self.assertEqual(self.i2v["16"]["inputs"]["crop"], "center")
        self.assertEqual(self.i2v["5"]["inputs"]["first_frame"], ["16", 0])

    def test_dimensions_are_on_h3_grid(self):
        for preset in self.manifest["presets"].values():
            for aspect in ("landscape", "portrait", "square"):
                width, height = preset[aspect]
                self.assertEqual(width % 32, 0)
                self.assertEqual(height % 32, 0)
                self.assertLessEqual(width * height, 1344 * 768)

    def test_continuation_routes_guide_into_conditioning(self):
        self.assertEqual(self.continuation["18"]["class_type"], "LoadVideo")
        self.assertEqual(self.continuation["19"]["class_type"], "GetVideoComponents")
        self.assertEqual(self.continuation["20"]["class_type"], "MiniMaxH3AddGuide")
        self.assertEqual(self.continuation["20"]["inputs"]["image"], ["19", 0])
        self.assertEqual(self.continuation["20"]["inputs"]["audio"], ["19", 1])
        self.assertEqual(self.continuation["20"]["inputs"]["frame_idx"], 0)
        self.assertEqual(self.continuation["9"]["inputs"]["conditioning"], ["20", 0])
        self.assertEqual(self.continuation["10"]["inputs"]["latent_image"], ["5", 1])

    def test_five_second_contract(self):
        self.assertEqual(self.t2v["5"]["inputs"]["length"], 124)
        self.assertEqual(self.i2v["5"]["inputs"]["length"], 124)
        self.assertEqual(self.continuation["5"]["inputs"]["length"], 124)
        self.assertEqual(self.manifest["duration"]["fps"], 24)


if __name__ == "__main__":
    unittest.main()
