---
title: Sample Note
tags: [test]
---

This is preamble text that appears before any heading. It belongs to the root
section and should become its own chunk with an empty heading path.

# Methods

Some introductory prose under the top-level Methods heading.

## Data Preprocessing

We resample every ECG lead to 250 Hz and apply a bandpass filter.

```python
# this heading-like comment is inside a code fence and must NOT be parsed
## not a heading
def preprocess(signal):
    return signal
```

### Normalization

Per-lead z-score normalization is applied after filtering.

## Model

A short section about the model.

# Results

A heading-light section follows that is deliberately long to exercise the
oversize splitter.
