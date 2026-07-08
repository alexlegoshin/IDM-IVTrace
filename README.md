# IVtrace – Automated I–V Characterization for Current Sensors

**IVtrace** is a Python script designed for automated acquisition of current–voltage (transfer) characteristics of current sensors. It runs in a Jupyter Notebook environment and communicates with SCPI‑compliant instruments via the NI‑VISA layer using the `pyvisa` library. Data handling and export are powered by `pandas`, and quick‑look plots can be generated with `matplotlib`.

## Key Features

- **Instrument auto‑detection** – works with AKIP‑2101 / Siglent multimeters and ITECH IT‑M3910D programmable DC power supplies.
- **Cached measurement settings** – all parameters (current range, step, voltage limit, delay, etc.) are stored in a JSON configuration file and reloaded on the next run.
- **Pulsed source operation** – the output of the current source is turned on only during the measurement of each point, reducing self‑heating and power dissipation in the sensor.
- **Manual polarity switching** – the script always sends a positive current to the unipolar source; the sign (positive/negative) is logically recorded in the results and metadata.
- **Rich CSV export** – the output file includes a full metadata header (test conditions, timestamp, number of points) followed by the measured data columns (`Timestamp`, `I_set_A`, `V_meas_V`).

## Typical Workflow

1. Load or enter test parameters (start/stop current, step, voltage limit, settling delay).
2. Choose the polarity branch (`positive` or `negative`) – short aliases `p`/`+` or `n`/`-` are supported.
3. The script automatically initializes both instruments.
4. For each current step:
   - Sets the (positive) current,
   - Turns on the output,
   - Waits for stabilization,
   - Acquires and averages 3 voltage readings,
   - Turns off the output,
   - Stores the timestamp, signed current, and averaged voltage.
5. At the end, the CSV file is saved with all metadata and data.
6. The first 10 rows are displayed in the console for verification.

## Requirements

- Python 3.8+
- `pyvisa`, `pandas`, `matplotlib`, `datetime`, `pathlib`, `json`
- NI‑VISA backend (or pyvisa‑py backend for pure‑Python communication)

## Repository Structure

- `IVtrace.ipynb` – main Jupyter Notebook
- `ivtrace_config.json` – auto‑generated cache for measurement parameters (created in `C:/IVTraceData/`)
- Sample CSV outputs in the same directory

## Usage Example

```bash
# Clone the repository
git clone https://github.com/yourusername/IVtrace.git
# Launch Jupyter and run the notebook
jupyter notebook IVtrace.ipynb
