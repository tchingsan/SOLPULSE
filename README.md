# SOLPULSE STABLE PAPER PILOT V12.2

V12.2 corrige le démarrage Windows.

## Erreur de conception supprimée

Dans V12 et V12.1, le lancement normal exécutait un achat synthétique dans une
base temporaire. Windows pouvait conserver brièvement un handle sur ce fichier
SQLite et produire `WinError 32`. Le lanceur arrêtait alors tout SOLPULSE, même
si le véritable dashboard et les moteurs n’avaient aucun problème.

V12.2 sépare définitivement les deux opérations :

```text
Démarrage normal
→ contrôles rapides non destructifs
→ moteurs
→ dashboard

Test d’achat manuel
→ base distincte dans data\diagnostics
→ aucun accès à la vraie base
→ aucun nettoyage immédiat du fichier SQLite
```

Le test synthétique ne peut plus empêcher SOLPULSE de démarrer.

## Lancer SOLPULSE

```text
01_START_SOLPULSE_STABLE_V12_2.bat
```

Dashboard :

```text
http://localhost:8527/?version=STABLE-PAPER-PILOT-V12-2
```

Le port 8527 évite les conflits avec les anciennes fenêtres V12/V12.1.

## Tester séparément l’achat paper

Fermer d’abord SOLPULSE, puis lancer :

```text
06_TESTER_ACHAT_PAPER_V12_2.bat
```

Le test crée une base indépendante dans :

```text
data\diagnostics
```

Il ne modifie jamais `data\trading.db` et conserve volontairement son fichier
de test afin d’éviter le verrouillage de suppression propre à Windows.

## Importer l’historique

1. Fermer toutes les anciennes versions.
2. Lancer `03_IMPORTER_BASE_PRECEDENTE.bat`.
3. Sélectionner la base de V12.1 ou de la version précédente.
4. Lancer `01_START_SOLPULSE_STABLE_V12_2.bat`.

Ne pas lancer `02_REINITIALISER_1_SOL.bat` pour conserver l’historique.

## Fonctionnement paper

- Paper Pilot : 0,01 SOL après le délai de validation événementielle ;
- acquisition complète : 0,05 SOL après Safety complet ;
- une seule position simultanée ;
- Mayhem toujours interdit ;
- aucune transaction réelle ;
- aucune clé privée requise.
