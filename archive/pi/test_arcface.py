#!/usr/bin/env python3
"""Quick de-risk: run ArcFace on the Hailo with a dummy face, confirm a 512-d
embedding comes out. Run with system python3."""
import numpy as np
from hailo_platform import (
    HEF, VDevice, ConfigureParams, InferVStreams,
    InputVStreamParams, OutputVStreamParams, HailoStreamInterface, FormatType,
)

HEF_PATH = "/usr/local/hailo/resources/models/hailo8/arcface_mobilefacenet.hef"

hef = HEF(HEF_PATH)
with VDevice() as target:
    cfg = ConfigureParams.create_from_hef(hef, interface=HailoStreamInterface.PCIe)
    ng = target.configure(hef, cfg)[0]
    ngp = ng.create_params()
    inp = InputVStreamParams.make(ng, format_type=FormatType.UINT8)
    outp = OutputVStreamParams.make(ng, format_type=FormatType.FLOAT32)
    in_info = hef.get_input_vstream_infos()[0]
    out_info = hef.get_output_vstream_infos()[0]
    dummy = np.random.randint(0, 255, (1, 112, 112, 3), dtype=np.uint8)
    with InferVStreams(ng, inp, outp) as pipe:
        with ng.activate(ngp):
            res = pipe.infer({in_info.name: dummy})
    emb = np.array(res[out_info.name]).flatten()
    print("embedding shape:", emb.shape)
    print("first 5:", emb[:5])
    print("L2 norm:", float(np.linalg.norm(emb)))
