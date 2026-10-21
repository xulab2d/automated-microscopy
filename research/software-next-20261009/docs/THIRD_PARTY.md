# Third-party numerical model attribution

The two-feature shape score in `flakefinder/colors.py` uses the numerical coefficients exported from `FalsePositiveDetector/models/classifier_L2_logistic.joblib` in [2DMatGMM](https://github.com/Jaluus/2DMatGMM), commit `e30c4d60fdb8f1dbbe79c53099261d7f2b67aca9`. Features are contour perimeter divided by square-root area, followed by solidity. The positive class is a flake-shaped object. Its score is not a probability calibrated on this microscope or on MoTe₂.

The original model was a scikit-learn logistic regression trained on labeled flake/other masks. Only its three numeric coefficients are used; no pickle loading or external hardware code occurs at application runtime.

Copyright (c) 2023 Jan-Lucas Uslu

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
