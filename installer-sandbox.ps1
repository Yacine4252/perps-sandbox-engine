# installer-sandbox.ps1  (moteur SANDBOX, ordres sur compte simule)
#
# Enregistre le moteur comme tache planifiee Windows : il repart tout seul
# au demarrage de la machine, et toutes les 10 minutes si le processus a
# disparu pour une raison quelconque.
#
# A lancer UNE SEULE FOIS, dans un PowerShell ouvert en ADMINISTRATEUR.
# Clic droit sur PowerShell -> "Executer en tant qu'administrateur".
#
# Pour desinstaller plus tard :
#     Unregister-ScheduledTask -TaskName "PerpsSandbox" -Confirm:$false

$ErrorActionPreference = "Stop"

$dossier = "C:\dev\perps-engine"
$python  = "$dossier\.venv\Scripts\python.exe"
$tache   = "PerpsSandbox"

# --- verifications avant d'enregistrer quoi que ce soit --------------------
if (-not (Test-Path $python)) {
    Write-Host "ERREUR : python introuvable a $python" -ForegroundColor Red
    Write-Host "Verifie que le dossier du projet est bien $dossier"
    exit 1
}

$estAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $estAdmin) {
    Write-Host "ERREUR : ce script doit tourner en administrateur." -ForegroundColor Red
    Write-Host "Ferme cette fenetre, clic droit sur PowerShell,"
    Write-Host "choisis 'Executer en tant qu'administrateur', et relance."
    exit 1
}

# --- la tache -------------------------------------------------------------
# -WindowStyle Hidden : le moteur tourne en arriere-plan, sans fenetre.
# Les journaux restent dans le dossier du projet, comme avant.
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m perps_engine.sandbox_run" `
    -WorkingDirectory $dossier

# Deux declencheurs : au demarrage, puis une verification reguliere.
$auDemarrage = New-ScheduledTaskTrigger -AtStartup

$surveillance = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 10)

# Reglages qui comptent :
#   AllowStartIfOnBatteries + DontStopIfGoingOnBatteries -> survit au debranchement
#   ExecutionTimeLimit 0                                 -> jamais tue pour duree
#   MultipleInstances IgnoreNew                          -> pas de doublon
$reglages = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

# SYSTEM : demarre meme si personne n'a ouvert de session apres le reboot.
$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

if (Get-ScheduledTask -TaskName $tache -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $tache -Confirm:$false
    Write-Host "Ancienne tache remplacee."
}

Register-ScheduledTask `
    -TaskName $tache `
    -Action $action `
    -Trigger @($auDemarrage, $surveillance) `
    -Settings $reglages `
    -Principal $principal `
    -Description "Moteur SANDBOX perps (ordres simules) - redemarre seul apres un reboot" | Out-Null

Write-Host ""
Write-Host "Tache '$tache' enregistree." -ForegroundColor Green
Write-Host ""
Write-Host "Le moteur repart maintenant tout seul :"
Write-Host "  - au demarrage de Windows"
Write-Host "  - dans les 10 minutes si le processus disparait"
Write-Host ""
Write-Host "Verifier l'etat      : Get-ScheduledTask -TaskName $tache"
Write-Host "Demarrer tout de suite : Start-ScheduledTask -TaskName $tache"
Write-Host "Arreter               : Stop-ScheduledTask -TaskName $tache"
Write-Host "Desinstaller          : Unregister-ScheduledTask -TaskName $tache -Confirm:`$false"
Write-Host ""
Write-Host "ATTENTION : ferme d'abord le moteur lance a la main (Ctrl+C)," -ForegroundColor Yellow
Write-Host "sinon deux copies ecriront dans les memes journaux." -ForegroundColor Yellow
