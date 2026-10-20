# agentbox: run a coding agent in an isolated container with only the current folder mounted.
# Usage: agentbox [flags] [command...]   (default: bash)   e.g. `agentbox -p python claude`, see --help

$usage = @'
Usage: agentbox [flags] [command...]   (default: bash)

Flags (before the command):
  -d, --docker         Docker inside the sandbox (privileged, see README)
      --php            PHP image (agent-box:php, or agent-box:php-docker with -d)
  -p, --presets LIST   mise toolchain presets, e.g. node,python
  -h, --help           show this help

e.g. agentbox --php -d -p node,python claude

PowerShell drops a bare --, so quote it: agentbox npm test "--" --watch
'@

# AGENTBOX_DOCKER=1 / AGENTBOX_PRESETS are the defaults, flags override them
$withDocker = $env:AGENTBOX_DOCKER -in @('1', 'true', 'yes')
$withPhp    = $false
$presets    = $env:AGENTBOX_PRESETS

# Flags end at the first non-flag, so `agentbox claude --resume` passes --resume to claude
$i = 0
for (; $i -lt $args.Count; $i++) {
  $arg = $args[$i]
  if ($arg -in @('-d', '--docker')) { $withDocker = $true }
  elseif ($arg -eq '--php') { $withPhp = $true }
  elseif ($arg -in @('-p', '--presets')) {
    if (++$i -ge $args.Count) { Write-Error "agentbox: $arg needs a value, e.g. node,python"; exit 2 }
    # Unquoted node,python arrives as an array in PowerShell
    $presets = $args[$i] -join ','
  }
  elseif ($arg -in @('-h', '--help')) { $usage; exit 0 }
  # Only reached with a quoted "--": PowerShell removes a bare -- before the script sees it
  elseif ($arg -eq '--') { $i++; break }
  elseif ($arg -is [string] -and $arg.StartsWith('-')) { Write-Error "agentbox: unknown flag $arg (see agentbox --help)"; exit 2 }
  else { break }
}

$engine     = if ($env:AGENTBOX_ENGINE)      { $env:AGENTBOX_ENGINE }      else { 'docker' }
# Image tag from the flags: agent-box, :php, :docker or :php-docker; AGENTBOX_IMAGE overrides it
$tags = @()
if ($withPhp)    { $tags += 'php' }
if ($withDocker) { $tags += 'docker' }
$image      = if ($env:AGENTBOX_IMAGE)       { $env:AGENTBOX_IMAGE }       elseif ($tags) { 'agent-box:' + ($tags -join '-') } else { 'agent-box' }
$homeVolume = if ($env:AGENTBOX_HOME_VOLUME) { $env:AGENTBOX_HOME_VOLUME } else { 'agent-box-home' }
$envFile    = if ($env:AGENTBOX_ENV_FILE)    { $env:AGENTBOX_ENV_FILE }    else { Join-Path $HOME '.config\agent-box\env' }

$project = (Get-Location).ProviderPath

# Mounting $HOME, a folder above it or a drive root would hand the agent ~/.ssh, cloud credentials and every project
$sep = [IO.Path]::DirectorySeparatorChar
$homeDir = (Resolve-Path $HOME).ProviderPath.TrimEnd($sep) + $sep
if ($homeDir.StartsWith($project.TrimEnd($sep) + $sep, [StringComparison]::OrdinalIgnoreCase) -or
    [IO.Path]::GetPathRoot($project) -eq $project) {
  Write-Error "agentbox: refusing to mount $project, it contains your home folder or a whole drive; cd into a project folder first"
  exit 2
}

# Names made safe for Docker: the folder name for the mount path, parent + folder for the
# container (C:\work\api: agent-box-work-api), and for the Docker-mode volume a hash of
# the full path, so two projects with the same folder name never share one
$name = (Split-Path -Leaf $project) -replace '[^a-zA-Z0-9_.-]', '-'
if (-not $name) { $name = 'root' }
$parentDir = Split-Path -Parent $project
$parent = if ($parentDir -and [IO.Path]::GetPathRoot($parentDir) -ne $parentDir) { (Split-Path -Leaf $parentDir) -replace '[^a-zA-Z0-9_.-]', '-' } else { '' }
$container = if ($parent) { "agent-box-$parent-$name" } else { "agent-box-$name" }
$hash = -join ([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($project))[0..3] | ForEach-Object { $_.ToString('x2') })
$dockerVolume = "agent-box-docker-$name-$hash"

$dockerArgs = @(
  'run', '-it', '--rm',
  '--name', $container,
  '--hostname', $container,
  '-v', "${homeVolume}:/home/agent-box",
  '-v', "${project}:/workspace/$name",
  '-w', "/workspace/$name"
)

if ($withDocker) {
  # Inner Docker's images and containers, one volume per project; 30s so dockerd
  # can stop its containers on docker stop (the default 10s kills it midway)
  $dockerArgs += @('--privileged', '--init', '--stop-timeout', '30', '-v', "${dockerVolume}:/var/lib/docker")
}

# Presets (java,go): mise toolchains, installed on first use
if ($presets) {
  $dockerArgs += @('-e', "MISE_ENV=$presets")
}

if (Test-Path $envFile) {
  $dockerArgs += @('--env-file', $envFile)
}

# Extra engine flags, split on whitespace, e.g. $env:AGENTBOX_OPTS = "-p 3000:3000"
if ($env:AGENTBOX_OPTS) {
  $dockerArgs += $env:AGENTBOX_OPTS -split '\s+' | Where-Object { $_ }
}

$command = if ($i -lt $args.Count) { $args[$i..($args.Count - 1)] } else { @('bash') }

& $engine @dockerArgs $image @command
exit $LASTEXITCODE
