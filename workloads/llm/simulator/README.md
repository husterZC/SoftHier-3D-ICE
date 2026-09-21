# Run-local simulator corrections

`build_model.py` copies the simulator sources and applies these patches with
zero fuzz. It builds separate model libraries using the existing native CMake
compiler, headers and ABI flags. Run-local GVSoC options select the component
models, and a run-local frontend selects the corrected engine libraries. No
original source or installed library is replaced.

- `0001-allow-subword-bank-access.patch`: DMA and accelerator interleavers can
  issue FP16 half-word requests. Accept nonempty accesses contained within one
  bank word, while retaining the original arbitration latency.
- `0002-initialize-collective-reductions.patch`: a reduction's DMA buffer can
  contain a previous transfer's data. Initialize each parent from its first
  returned child, then combine subsequent responses. This also supports maxima
  over negative values without an artificial zero identity. Release the child
  arrays with `delete[]`, matching their allocation.

- `0003-zero-idle-power-roundoff.patch`: adding and removing child power quanta
  can leave a small negative dynamic-power residue at idle. Clear negative
  residues strictly between −1 pW and zero before they can reduce accumulated
  energy. Preserve positive power and larger negative values, so the existing
  finite-state and monotonic-energy checks continue to catch real failures.
  The four engine variants are relinked with this one corrected source and
  their existing native objects; the ABI is unchanged. Link inputs and hashes
  are recorded in `native/engine-build.json`.

The package also supplies `**/vu/lsu_width=4`: the architecture specifies a
32-bit Spatz port, while the LSU property expects bytes. Vector-register length
and the architecture's declared width stay unchanged.

The runner verifies that every vector unit and bank arbiter, and the data NoC,
use the expected configuration. Source, library and effective-configuration
hashes are archived with each run. The native CMake build tree is required;
recreate it with `--build-hardware` if it has been removed.
