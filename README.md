# SOLPULSE V12.2

> **Système de trading algorithmique en Python sur la blockchain Solana, comprenant l’analyse de données de marché, l’automatisation des ordres, le suivi des performances et la gestion du risque via un dashboard interactif.**

## Présentation

**SOLPULSE** est une plateforme de trading algorithmique conçue pour détecter, analyser et simuler des opérations sur des tokens de l’écosystème Solana.

Le projet repose sur une architecture événementielle capable de surveiller les nouveaux lancements, d’évaluer les risques associés à chaque token et d’exécuter automatiquement des ordres sur la blockchain Solana.

L’objectif est de disposer d’une infrastructure complète permettant de tester, mesurer et améliorer une stratégie avant toute utilisation avec des fonds réels.

---

## Fonctionnalités principales

### Détection en temps réel

- Connexion WebSocket au réseau Solana
- Détection des nouveaux tokens dès leur création
- Décodage des événements on-chain
- Suivi des bonding curves et des migrations vers les DEX
- Identification immédiate des tokens utilisant le Mayhem Mode

### Analyse de marché

- Suivi du prix, de la liquidité et du volume
- Analyse de la progression des bonding curves
- Observation de l’activité d’achat et de vente
- Classement dynamique des tokens selon leur proximité avec les conditions d’entrée
- Collecte d’échantillons pour l’analyse historique et le backtesting

### Safety Engine

- Vérification des mint et freeze authorities
- Analyse de la distribution des holders
- Regroupement des comptes appartenant à un même wallet
- Exclusion des pools de liquidité dans le calcul de concentration
- Contrôle du plus gros holder hors pool
- Exclusion automatique des tokens Mayhem
- Blocage des actifs présentant un risque critique

### Automatisation des ordres

SOLPULSE dispose de deux modes d’entrée en paper trading :

- **Paper Pilot** : entrée technique de `0,01 SOL` pour valider rapidement l’ensemble du pipeline
- **Acquisition complète** : entrée de `0,05 SOL` après validation des contrôles de sécurité

Le moteur garantit qu’une seule position peut être ouverte simultanément.

### Gestion du risque

- Stop-loss configurable
- Take-profit automatique
- Break-even dynamique
- Durée maximale de détention
- Limitation de l’exposition totale
- Réserve minimale de capital
- Fermeture automatique en cas de dégradation du niveau de sécurité

### Suivi des performances

- Capital disponible et valeur totale du portefeuille
- PnL réalisé et latent
- Taux de réussite
- Historique des positions
- Historique des ordres simulés
- Courbe d’équité
- Mesure du drawdown
- Replay des événements et backtesting

---

## Dashboard interactif

L’interface Streamlit centralise les principales informations du système :

- état des moteurs en temps réel ;
- tokens récemment détectés ;
- classement des opportunités ;
- résultats des analyses de sécurité ;
- positions ouvertes ;
- historique des transactions ;
- performances du portefeuille ;
- diagnostics techniques ;
- erreurs RPC et limitations des fournisseurs de données.

Le dashboard a été conçu pour rendre le fonctionnement du système compréhensible sans avoir à consulter directement les journaux ou la base de données.

---

## Architecture technique

```text
Solana WebSocket / Pump Events
              ↓
       New Coin Radar
              ↓
      Market Data Engine
              ↓
         Safety Engine
              ↓
    Qualification Pipeline
              ↓
     Paper Trading Engine
              ↓
    Portfolio & Risk Manager
              ↓
   SQLite / Replay / Backtest
              ↓
     Dashboard Streamlit
