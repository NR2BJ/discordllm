using System.Collections.Concurrent;
using System.Text.Json;
using Discord;
using Discord.WebSocket;

const int StartupRestoreDelayMs = 5000;
const int ReconcileIntervalMs = 30000;
const int ReconnectDelayMs = 5000;
const int FailedConnectCooldownMs = 20000;
const int AudioRecoveryDelayMs = 1500;
const int AudioRecoveryCooldownMs = 15000;

var token = Environment.GetEnvironmentVariable("BAEKSU_DISCORD_TOKEN")
    ?? throw new InvalidOperationException("BAEKSU_DISCORD_TOKEN is required");
var configFile = Environment.GetEnvironmentVariable("CONFIG_FILE")
    ?? Path.Combine(AppContext.BaseDirectory, "baeksu.json");
var hardAudioRecovery = ParseBoolEnv("BAEKSU_AUDIO_HARD_RECOVERY", defaultValue: false);

ConfigureDaveLogging();
Console.WriteLine(
    $"[BAEKSU] audio hard recovery {(hardAudioRecovery ? "enabled" : "disabled")} (BAEKSU_AUDIO_HARD_RECOVERY)"
);

var client = new DiscordSocketClient(new DiscordSocketConfig
{
    GatewayIntents = GatewayIntents.Guilds | GatewayIntents.GuildVoiceStates | GatewayIntents.GuildMembers,
    LogGatewayIntentWarnings = false,
    AlwaysDownloadUsers = true,
    EnableVoiceDaveEncryption = true,
});

var state = new StayVcState(configFile);
var reconnectLocks = new ConcurrentDictionary<ulong, SemaphoreSlim>();
var failedConnectCooldowns = new ConcurrentDictionary<ulong, DateTimeOffset>();
var audioRecoveryCooldowns = new ConcurrentDictionary<ulong, DateTimeOffset>();
Task? reconcileTask = null;

client.Log += log =>
{
    if (ShouldSuppressClientLog(log))
    {
        return Task.CompletedTask;
    }

    Console.WriteLine($"[BAEKSU] {log}");
    if (ShouldRecoverFromAudioLog(log))
    {
        QueueAudioRecoveryAsync($"audio-log:{log.Source}");
    }

    return Task.CompletedTask;
};

client.Ready += async () =>
{
    Console.WriteLine($"[BAEKSU] ready as {client.CurrentUser}");
    await RegisterCommandsAsync();

    _ = Task.Run(async () =>
    {
        await Task.Delay(StartupRestoreDelayMs);
        foreach (var guild in client.Guilds)
        {
            if (state.GetTargetChannelId(guild.Id) is ulong targetChannelId)
            {
                await EnsureConnectedAsync(guild.Id, targetChannelId, "startup");
            }
        }
    });

    reconcileTask ??= Task.Run(async () =>
    {
        using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(ReconcileIntervalMs));
        while (await timer.WaitForNextTickAsync())
        {
            foreach (var guild in client.Guilds)
            {
                if (state.GetTargetChannelId(guild.Id) is not ulong targetChannelId)
                {
                    continue;
                }

                if (guild.CurrentUser?.VoiceChannel?.Id == targetChannelId)
                {
                    continue;
                }

                await EnsureConnectedAsync(guild.Id, targetChannelId, "reconcile");
            }
        }
    });
};

client.JoinedGuild += async guild =>
{
    await RegisterCommandsAsync(guild);
};

client.SlashCommandExecuted += async command =>
{
    if (command.GuildId is not ulong guildId)
    {
        await command.RespondAsync("서버에서만 사용 가능합니다.", ephemeral: true);
        return;
    }

    switch (command.Data.Name)
    {
        case "vcjoin":
        {
            var member = command.User as SocketGuildUser;
            var channel = member?.VoiceChannel;
            if (channel is null)
            {
                await command.RespondAsync("음성 채널에 먼저 들어가 주세요.", ephemeral: true);
                return;
            }

            await command.DeferAsync(ephemeral: true);
            state.SetTargetChannelId(guildId, channel.Id);
            QueueSlashConnectAsync(
                command,
                guildId,
                channel.Id,
                "slash:vcjoin",
                $"상주 시작: {channel.Mention}",
                "연결 시도는 했지만 음성 채널 연결을 확인하지 못했습니다."
            );
            return;
        }
        case "vcleave":
        {
            await command.DeferAsync(ephemeral: true);
            state.ClearTargetChannelId(guildId);
            var guild = client.GetGuild(guildId);
            var current = guild?.CurrentUser?.VoiceChannel;
            if (current is not null)
            {
                await Task.Run(async () => await current.DisconnectAsync());
            }

            await command.FollowupAsync("상주 채널을 해제했습니다.", ephemeral: true);
            return;
        }
        case "vcrejoin":
        {
            var targetChannelId = state.GetTargetChannelId(guildId);
            if (targetChannelId is null)
            {
                await command.RespondAsync("저장된 상주 채널이 없습니다.", ephemeral: true);
                return;
            }

            await command.DeferAsync(ephemeral: true);
            QueueSlashConnectAsync(
                command,
                guildId,
                targetChannelId.Value,
                "slash:vcrejoin",
                "저장된 채널로 다시 붙었습니다.",
                "재접속을 시도했지만 연결을 확인하지 못했습니다."
            );
            return;
        }
        case "vcstatus":
        {
            var guild = client.GetGuild(guildId);
            var targetChannelId = state.GetTargetChannelId(guildId);
            var currentChannelId = guild?.CurrentUser?.VoiceChannel?.Id;
            var currentChannelName = guild?.CurrentUser?.VoiceChannel?.Name ?? "none";
            await command.RespondAsync(
                $"targetChannelId: {(targetChannelId?.ToString() ?? "none")}\n" +
                $"currentChannelId: {(currentChannelId?.ToString() ?? "none")}\n" +
                $"currentChannelName: {currentChannelName}",
                ephemeral: true
            );
            return;
        }
    }
};

client.UserVoiceStateUpdated += async (user, before, after) =>
{
    if (user is not SocketGuildUser guildUser || user.Id != client.CurrentUser.Id)
    {
        return;
    }

    var guildId = guildUser.Guild.Id;
    Console.WriteLine(
        $"[BAEKSU] guild={guildId} voice {FormatVoiceChannelId(before)} -> {FormatVoiceChannelId(after)}"
    );

    if (after.VoiceChannel is not null)
    {
        state.SetTargetChannelId(guildId, after.VoiceChannel.Id);
        return;
    }

    var targetChannelId = state.GetTargetChannelId(guildId);
    if (targetChannelId is null)
    {
        return;
    }

    _ = Task.Run(async () =>
    {
        await Task.Delay(ReconnectDelayMs);
        if (state.GetTargetChannelId(guildId) is ulong latestTargetChannelId && latestTargetChannelId == targetChannelId.Value)
        {
            await EnsureConnectedAsync(guildId, targetChannelId.Value, "voice-state");
        }
    });
};

await client.LoginAsync(TokenType.Bot, token);
await client.StartAsync();
await Task.Delay(Timeout.Infinite);

static void ConfigureDaveLogging()
{
    try
    {
        var minRank = DaveLogRank(Environment.GetEnvironmentVariable("BAEKSU_DAVE_LOG_LEVEL") ?? "Warning");
        Discord.LibDave.Dave.SetLogSink((severity, filePath, lineNumber, message) =>
        {
            if (DaveLogRank(severity.ToString()) < minRank)
            {
                return;
            }
            var fileName = Path.GetFileName(filePath);
            Console.WriteLine($"[BAEKSU][LIBDAVE:{severity}] {fileName}#{lineNumber} {message}");
        });
        Console.WriteLine($"[BAEKSU] libdave log level >= {DaveLogName(minRank)}");
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[BAEKSU] failed to configure libdave log sink: {ex.Message}");
    }
}

static int DaveLogRank(string? severity)
{
    return severity switch
    {
        "Verbose" => 0,
        "Info" => 1,
        "Warning" or "Warn" => 2,
        "Error" => 3,
        "Critical" or "Fatal" => 4,
        _ => 2,
    };
}

static string DaveLogName(int rank)
{
    return rank switch
    {
        <= 0 => "Verbose",
        1 => "Info",
        2 => "Warning",
        3 => "Error",
        _ => "Critical",
    };
}

static string FormatVoiceChannelId(SocketVoiceState state)
{
    return state.VoiceChannel is null ? "none" : state.VoiceChannel.Id.ToString();
}

void QueueSlashConnectAsync(
    SocketSlashCommand command,
    ulong guildId,
    ulong channelId,
    string reason,
    string successMessage,
    string failureMessage
)
{
    _ = Task.Run(async () =>
    {
        var ok = await EnsureConnectedAsync(guildId, channelId, reason, bypassCooldown: true);
        try
        {
            await command.FollowupAsync(ok ? successMessage : failureMessage, ephemeral: true);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[BAEKSU] followup failed guild={guildId} reason={reason}: {ex.Message}");
        }
    });
}

void QueueAudioRecoveryAsync(string reason)
{
    _ = Task.Run(async () =>
    {
        await Task.Delay(AudioRecoveryDelayMs);
        foreach (var guild in client.Guilds)
        {
            if (state.GetTargetChannelId(guild.Id) is not ulong targetChannelId)
            {
                continue;
            }

            if (audioRecoveryCooldowns.TryGetValue(guild.Id, out var cooldownUntil)
                && cooldownUntil > DateTimeOffset.UtcNow)
            {
                Console.WriteLine(
                    $"[BAEKSU] guild={guild.Id} skip audio recovery cooldown-until={cooldownUntil:O} reason={reason}"
                );
                continue;
            }

            audioRecoveryCooldowns[guild.Id] = DateTimeOffset.UtcNow.AddMilliseconds(AudioRecoveryCooldownMs);
            await RecoverAudioConnectionAsync(guild.Id, targetChannelId, reason);
        }
    });
}

async Task RecoverAudioConnectionAsync(ulong guildId, ulong channelId, string reason)
{
    var guild = client.GetGuild(guildId);
    if (guild?.CurrentUser?.VoiceChannel?.Id == channelId)
    {
        if (!hardAudioRecovery)
        {
            Console.WriteLine(
                $"[BAEKSU] guild={guildId} audio disconnected but voice state is still in target; waiting for voice-state/reconcile reason={reason}"
            );
            return;
        }
    }

    await EnsureConnectedAsync(
        guildId,
        channelId,
        hardAudioRecovery ? $"{reason}:hard-reconnect" : reason,
        bypassCooldown: true,
        forceReconnect: hardAudioRecovery
    );
}

static bool ShouldSuppressClientLog(LogMessage log)
{
    if (!log.Source.StartsWith("Dave", StringComparison.OrdinalIgnoreCase))
    {
        return false;
    }

    var message = log.Message ?? "";
    return message.Contains("Failed to decrypt audio packet", StringComparison.OrdinalIgnoreCase)
        || message.Contains("Unexpected transition id", StringComparison.OrdinalIgnoreCase);
}

static bool ShouldRecoverFromAudioLog(LogMessage log)
{
    if (!log.Source.StartsWith("Audio", StringComparison.OrdinalIgnoreCase) || log.Exception is null)
    {
        return false;
    }

    var text = $"{log.Message} {log.Exception.GetType().Name} {log.Exception.Message}";
    return text.Contains("WebSocket", StringComparison.OrdinalIgnoreCase)
        || text.Contains("connection was closed", StringComparison.OrdinalIgnoreCase)
        || text.Contains("NullReferenceException", StringComparison.OrdinalIgnoreCase);
}

async Task RegisterCommandsAsync(SocketGuild? onlyGuild = null)
{
    IReadOnlyCollection<SocketGuild> guilds = onlyGuild is null
        ? client.Guilds
        : new List<SocketGuild> { onlyGuild };
    foreach (var guild in guilds)
    {
        try
        {
            var commands = new List<ApplicationCommandProperties>
            {
                new SlashCommandBuilder().WithName("vcjoin").WithDescription("현재 들어가 있는 음성 채널에 상주합니다.").Build(),
                new SlashCommandBuilder().WithName("vcleave").WithDescription("음성 채널에서 나가고 상주를 해제합니다.").Build(),
                new SlashCommandBuilder().WithName("vcstatus").WithDescription("현재 상주 상태와 음성 연결 상태를 확인합니다.").Build(),
                new SlashCommandBuilder().WithName("vcrejoin").WithDescription("저장된 상주 채널로 강제 재접속합니다.").Build(),
            };
            await guild.BulkOverwriteApplicationCommandAsync(commands.ToArray());
            Console.WriteLine($"[BAEKSU] registered commands for guild={guild.Id} ({guild.Name})");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[BAEKSU] command registration failed guild={guild.Id}: {ex.Message}");
        }
    }
}

async Task<bool> EnsureConnectedAsync(
    ulong guildId,
    ulong channelId,
    string reason,
    bool bypassCooldown = false,
    bool forceReconnect = false
)
{
    var gate = reconnectLocks.GetOrAdd(guildId, _ => new SemaphoreSlim(1, 1));
    await gate.WaitAsync();
    try
    {
        if (state.GetTargetChannelId(guildId) is not ulong latestTargetChannelId || latestTargetChannelId != channelId)
        {
            Console.WriteLine($"[BAEKSU] guild={guildId} skip connect stale-target channel={channelId} reason={reason}");
            return false;
        }

        var guild = client.GetGuild(guildId);
        if (guild is null)
        {
            Console.WriteLine($"[BAEKSU] guild={guildId} unavailable reason={reason}");
            return false;
        }

        var channel = guild.GetVoiceChannel(channelId);
        if (channel is null)
        {
            Console.WriteLine($"[BAEKSU] guild={guildId} target channel unavailable channel={channelId} reason={reason}");
            return false;
        }

        if (!bypassCooldown
            && failedConnectCooldowns.TryGetValue(guildId, out var cooldownUntil)
            && cooldownUntil > DateTimeOffset.UtcNow)
        {
            Console.WriteLine(
                $"[BAEKSU] guild={guildId} skip connect cooldown-until={cooldownUntil:O} channel={channelId} reason={reason}"
            );
            return false;
        }

        var currentChannel = guild.CurrentUser?.VoiceChannel;
        if (currentChannel?.Id == channelId && !forceReconnect)
        {
            failedConnectCooldowns.TryRemove(guildId, out _);
            return true;
        }

        if (forceReconnect && currentChannel is not null)
        {
            try
            {
                Console.WriteLine(
                    $"[BAEKSU] guild={guildId} recycling voice connection current={currentChannel.Id} target={channelId} reason={reason}"
                );
                await currentChannel.DisconnectAsync();
                await Task.Delay(1000);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[BAEKSU] guild={guildId} recycle disconnect failed reason={reason}: {ex.Message}");
            }
        }

        try
        {
            Console.WriteLine($"[BAEKSU] guild={guildId} joining channel={channelId} ({channel.Name}) reason={reason}");
            await channel.ConnectAsync(selfDeaf: true, selfMute: false, external: false, disconnect: true);
            state.SetTargetChannelId(guildId, channelId);
            failedConnectCooldowns.TryRemove(guildId, out _);
            return true;
        }
        catch (Exception ex)
        {
            failedConnectCooldowns[guildId] = DateTimeOffset.UtcNow.AddMilliseconds(FailedConnectCooldownMs);
            Console.WriteLine($"[BAEKSU] guild={guildId} connect failed channel={channelId} reason={reason}: {ex.Message}");
            return false;
        }
    }
    finally
    {
        gate.Release();
    }
}

static bool ParseBoolEnv(string name, bool defaultValue)
{
    var value = Environment.GetEnvironmentVariable(name);
    if (string.IsNullOrWhiteSpace(value))
    {
        return defaultValue;
    }

    return value.Equals("1", StringComparison.OrdinalIgnoreCase)
        || value.Equals("true", StringComparison.OrdinalIgnoreCase)
        || value.Equals("yes", StringComparison.OrdinalIgnoreCase)
        || value.Equals("on", StringComparison.OrdinalIgnoreCase);
}

sealed class StayVcState
{
    private readonly string _configFile;
    private readonly object _sync = new();
    private StateModel _data;

    public StayVcState(string configFile)
    {
        _configFile = configFile;
        Directory.CreateDirectory(Path.GetDirectoryName(_configFile)!);
        _data = Load();
    }

    public ulong? GetTargetChannelId(ulong guildId)
    {
        lock (_sync)
        {
            return _data.Guilds.TryGetValue(guildId.ToString(), out var guild) ? guild.TargetChannelId : null;
        }
    }

    public void SetTargetChannelId(ulong guildId, ulong channelId)
    {
        lock (_sync)
        {
            _data.Guilds[guildId.ToString()] = new GuildState
            {
                TargetChannelId = channelId,
                UpdatedAt = DateTimeOffset.UtcNow,
            };
            Save();
        }
    }

    public void ClearTargetChannelId(ulong guildId)
    {
        lock (_sync)
        {
            if (_data.Guilds.Remove(guildId.ToString()))
            {
                Save();
            }
        }
    }

    private StateModel Load()
    {
        try
        {
            if (!File.Exists(_configFile))
            {
                return new StateModel();
            }

            var json = File.ReadAllText(_configFile);
            return JsonSerializer.Deserialize<StateModel>(json) ?? new StateModel();
        }
        catch
        {
            return new StateModel();
        }
    }

    private void Save()
    {
        var json = JsonSerializer.Serialize(_data, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(_configFile, json);
    }
}

sealed class StateModel
{
    public Dictionary<string, GuildState> Guilds { get; set; } = new();
}

sealed class GuildState
{
    public ulong? TargetChannelId { get; set; }
    public DateTimeOffset UpdatedAt { get; set; }
}
