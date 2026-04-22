using System.Collections.Concurrent;
using System.Text.Json;
using Discord;
using Discord.WebSocket;

const int StartupRestoreDelayMs = 5000;
const int ReconcileIntervalMs = 30000;
const int ReconnectDelayMs = 5000;

var token = Environment.GetEnvironmentVariable("BAEKSU_DISCORD_TOKEN")
    ?? throw new InvalidOperationException("BAEKSU_DISCORD_TOKEN is required");
var configFile = Environment.GetEnvironmentVariable("CONFIG_FILE")
    ?? Path.Combine(AppContext.BaseDirectory, "baeksu.json");

var client = new DiscordSocketClient(new DiscordSocketConfig
{
    GatewayIntents = GatewayIntents.Guilds | GatewayIntents.GuildVoiceStates,
    LogGatewayIntentWarnings = false,
    AlwaysDownloadUsers = false,
});

var state = new StayVcState(configFile);
var reconnectLocks = new ConcurrentDictionary<ulong, SemaphoreSlim>();
Task? reconcileTask = null;

client.Log += message =>
{
    Console.WriteLine($"[BAEKSU] {message}");
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
            var ok = await EnsureConnectedAsync(guildId, channel.Id, "slash:vcjoin");
            await command.FollowupAsync(
                ok ? $"상주 시작: {channel.Mention}" : "연결 시도는 했지만 음성 채널 연결을 확인하지 못했습니다.",
                ephemeral: true
            );
            return;
        }
        case "vcleave":
        {
            state.ClearTargetChannelId(guildId);
            var guild = client.GetGuild(guildId);
            var current = guild?.CurrentUser?.VoiceChannel;
            if (current is not null)
            {
                await current.DisconnectAsync();
            }

            await command.RespondAsync("상주를 해제하고 음성 채널에서 나갔습니다.", ephemeral: true);
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
            var ok = await EnsureConnectedAsync(guildId, targetChannelId.Value, "slash:vcrejoin");
            await command.FollowupAsync(
                ok ? "저장된 채널로 다시 붙었습니다." : "재접속을 시도했지만 연결을 확인하지 못했습니다.",
                ephemeral: true
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
        $"[BAEKSU] guild={guildId} voice {before.VoiceChannel?.Id?.ToString() ?? "none"} -> {after.VoiceChannel?.Id?.ToString() ?? "none"}"
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
        await EnsureConnectedAsync(guildId, targetChannelId.Value, "voice-state");
    });
};

await client.LoginAsync(TokenType.Bot, token);
await client.StartAsync();
await Task.Delay(Timeout.Infinite);

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

async Task<bool> EnsureConnectedAsync(ulong guildId, ulong channelId, string reason)
{
    var gate = reconnectLocks.GetOrAdd(guildId, _ => new SemaphoreSlim(1, 1));
    await gate.WaitAsync();
    try
    {
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

        if (guild.CurrentUser?.VoiceChannel?.Id == channelId)
        {
            return true;
        }

        try
        {
            Console.WriteLine($"[BAEKSU] guild={guildId} joining channel={channelId} ({channel.Name}) reason={reason}");
            await channel.ConnectAsync(selfDeaf: true, selfMute: false, external: false, disconnect: true);
            state.SetTargetChannelId(guildId, channelId);
            return true;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[BAEKSU] guild={guildId} connect failed channel={channelId} reason={reason}: {ex.Message}");
            return false;
        }
    }
    finally
    {
        gate.Release();
    }
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
