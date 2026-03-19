# anon0mesh

**Mesh First, Chain When It Matters.**

anon0mesh is a Python MVP for tunneling Solana JSON-RPC requests over [Reticulum's](https://reticulum.network/) end-to-end encrypted mesh network. It allows off-grid devices to seamlessly interact with the Solana blockchain through connected gateway nodes ("Beacons") using almost any transport medium (LoRa, BLE, WiFi, Packet Radio, etc.).

## 🏗 Architecture

The system consists of two primary components:

*   **Beacon (`beacon.py`) - The Gateway**: Runs on a high-uptime internet-connected device. It acts as a Reticulum destination, actively listening for encrypted RPC requests from the mesh. When a request is received, it forwards it to a Solana RPC endpoint (devnet/mainnet) and sends the response back into the mesh.
*   **Client (`client.py`) - The Off-grid Wallet**: Runs on an offline device (e.g., a laptop or mobile device in a remote area). It discovers available Beacons over the mesh, optionally signs transactions locally, and transmits requests entirely over Reticulum without needing direct internet access.

## 🚀 Quickstart

A comprehensive setup script is included to handle dependencies (including Python venvs), configure Reticulum hubs, and manage launcher scripts or systemd services.

```bash
# Clone the repository
git clone https://github.com/Magicred-1/anon0mesh_cli.git
cd anon0mesh_cli

# Run the interactive setup script
chmod +x setup.sh
./setup.sh
```

### Setup Script Options (Non-interactive)
You can bypass the interactive menu by using CLI flags:
```bash
./setup.sh --beacon     # Install node dependencies & launcher only
./setup.sh --client     # Install client tools & offline signer dependencies
./setup.sh --both       # Install both sets of tools
./setup.sh --systemd    # (Optional) Install the beacon as a systemd service
./setup.sh --ble        # (Optional) Add Bluetooth Low Energy support
./setup.sh --mainnet    # Set the default Solana network to Mainnet
```

## 🛠 Usage

After running the setup script, two launcher scripts will be created in your directory: `run_beacon.sh` and `run_client.sh`.

### 1. Starting a Beacon
Run the beacon script on your internet-connected device:
```bash
./run_beacon.sh
```
*Note: The beacon will output a Reticulum DESTINATION HASH on startup. This is its unique routing address on the mesh.*

### 2. Running a Client
Run the client on your offline or mesh-only device. The default setup includes auto-discovery mode to easily find local beacons:

```bash
# Auto-discover local beacons and start interactive client
./run_client.sh

# Connect manually to a specific beacon hash
./run_client.sh <BEACON_HASH>

# One-shot Solana balance check against a specific beacon
./run_client.sh <BEACON_HASH> --balance <SOLANA_WALLET_ADDRESS>
```

## 📦 Dependencies

The core project uses:
*   `rns` (Reticulum Network Stack)
*   `lxmf` (Lightweight Extensible Message Format for discovery)
*   `requests` (For Solana RPC calls)
*   `solders` (Optional; for client-side offline transaction signing)
*   `bleak` / `meshtastic` (Optional; for specific transport interfaces)

*All dependencies are automatically managed by `setup.sh` inside a local python virtual environment (`venv/`).*
