---
tags:
- computer-vision
- object-detection
- yolo11s
- fruits
library_name: ultralytics
license: cc-by-4.0
datasets:
- Johnatanvq/fruitsdata
---

# Fruits Detection Models (YOLOv11 + OAK Deployment)

This repository provides two versions of a YOLO-based model trained to detect **apples, carrots, and oranges**.  
The models were trained on the [Fruits Dataset](https://huggingface.co/datasets/johnatanvq/fruits-dataset), which contains **160 annotated images** with variations in **angles, distances, lighting, shadows, quantities, and surfaces**.

---

## Repository Structure

fruits-yolo-model/ </br>
├── my_model_PC/ </br>
│ └── my_model.pt </br>
└── my_model_CAMERA/ </br>
├── my_model_openvino_2022.1_6shave.blob </br>
├── my_model-simplified.onnx </br>
├── my_model.bin </br>
├── my_model.xml </br>
└── my_model.json </br>
---

## Training & Conversion
Training: The model was trained with the Fruits Dataset.

Conversion: The .pt weights were exported to ONNX and then converted via Luxonis tools into the .blob format for OAK deployment.

Source Code: Training scripts and conversion pipeline are documented here:
GitHub: [fruit_detection_model](https://github.com/Johnatanvq/fruit_detection_model)  

## License
This model is released under the CC-BY 4.0 license.
You are free to share, use, and adapt the models, including for commercial purposes, as long as you provide proper attribution.

## Attribution
If you use these models, please cite them as:

*Fruits Detection Models (YOLOv11 + OAK Deployment), by **Johnatan Valencia ([johnatanvq on Github](https://github.com/Johnatanvq))**, trained on the Fruits Dataset, licensed under CC-BY 4.0.*

## Notes
The dataset is compact (160 images) but provides strong variation for robust training.

my_model.pt is suitable for PyTorch inference and further training.

my_model_openvino_2022.1_6shave.blob is optimized for real-time inference on OAK devices.

Supporting files (.onnx, .bin, .xml, .json) are included for reproducibility.