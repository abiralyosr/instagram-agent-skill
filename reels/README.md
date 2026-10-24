# Projets de Reels

Un dossier par Reel : `reels/<marque>/<date>-<titre>/`. Il est créé avec :

```bash
python3 skills/ig-montage/reelforge.py new abir-atib "lancement oud"
python3 skills/ig-montage/reelforge.py new vitaya-relax "routine du soir"
```

## Ce qui est gardé dans git, et ce qui ne l'est pas

| Gardé (la recette) | Pas gardé (trop lourd, à garder ailleurs) |
|---|---|
| `edit.json` : le montage, plan par plan | `inputs/` : rushes, photos, voix, musique |
| `script.txt` : le texte des sous-titres | `out/` : planches contact, fichiers de travail, exports |
| `subs.srt` : sous-titres calés et corrigés | |

Avec la recette et les fichiers d'origine, n'importe quelle session peut
refaire le même montage à l'identique. **Les originaux et les vidéos finales
doivent rester chez toi** (téléphone, Google Drive...) : le conteneur cloud est
effacé à la fin de la session.

## Étapes

1. `new` : crée le dossier
2. déposer les fichiers dans `inputs/video`, `inputs/photo`, `inputs/voice`, `inputs/music`
3. `probe` : inventaire et planches contact
4. `subs` : sous-titres calés sur la voix, puis relecture
5. `plan` puis montage réel dans `edit.json`
6. `check` : contrôle qualité avant rendu
7. `render --preview` : version de relecture 540×960
8. `render --final` : master 1080×1920
