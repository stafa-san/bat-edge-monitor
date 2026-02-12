# 🦇 Soundscape Monitor

Real-time acoustic monitoring system using AudioMoth, Raspberry Pi 5, and deep learning for ecological research.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 Raspberry Pi 5                   │
│                                                  │
│  ┌──────────┐  ┌──────────────┐  ┌───────────┐ │
│  │ AudioMoth │→│ AST Service  │→│           │ │
│  │ (192kHz)  │  │ (soundscape) │  │ PostgreSQL│ │
│  │           │→│ BatDetect2   │→│ (local)   │ │
│  │           │  │ (bat calls)  │  │           │ │
│  └──────────┘  └──────────────┘  └─────┬─────┘ │
│                                        │        │
│                              ┌─────────▼──────┐ │
│                              │  Sync Service  │ │
│                              └────────┬───────┘ │
└───────────────────────────────────────┼─────────┘
                                        │
                              ┌─────────▼──────────┐
                              │  Firebase Firestore │
                              └─────────┬──────────┘
                                        │
                              ┌─────────▼──────────┐
                              │  Next.js Dashboard  │
                              │  (Vercel)           │
                              └────────────────────┘
```

## Components

### Edge (Raspberry Pi 5)
- **AST Service**: Audio Spectrogram Transformer for general soundscape classification (527 categories)
- **BatDetect2 Service**: Specialized bat echolocation detection and species identification
- **Sync Service**: Pushes local PostgreSQL data to Firebase Firestore

### Cloud
- **Firebase Firestore**: Cloud database for classification results
- **Next.js Dashboard**: Real-time visualization deployed on Vercel

## Hardware
- Raspberry Pi 5 (4GB+)
- AudioMoth (USB Microphone firmware v1.3.1, 192kHz)
- USB Micro-B to USB-A cable

## Quick Start

### Edge (on Raspberry Pi)
```bash
cd edge
docker compose up --build
```

### Dashboard (local development)
```bash
cd dashboard
npm install
npm run dev
```

## Research Context
Part of thesis: *"Beyond Single Sensors: Quantifying Data Integrity in Multi-modal Edge Systems for Real-Time Ecological Monitoring"*

## References
- Adamiak, M. (2025). Audio Spectrogram Transformers Beyond the Lab.
- Mac Aodha, O. et al. (2022). Towards a General Approach for Bat Echolocation Detection and Classification.
- Gong, Y. et al. (2021). AST: Audio Spectrogram Transformer.
