const fs = require('node:fs');
const path = require('node:path');
const {
  ChannelType,
  Client,
  Events,
  GatewayIntentBits,
  REST,
  Routes,
  SlashCommandBuilder,
} = require('discord.js');
const {
  VoiceConnectionStatus,
  entersState,
  getVoiceConnection,
  joinVoiceChannel,
} = require('@discordjs/voice');

const DISCORD_TOKEN = process.env.STAYVC_DISCORD_TOKEN;
const CONFIG_FILE = process.env.CONFIG_FILE || path.join(__dirname, 'server.json');
const READY_TIMEOUT_MS = 30_000;
const TRANSIENT_DISCONNECT_GRACE_MS = 5_000;
const STARTUP_RESTORE_DELAY_MS = 5_000;
const RECONCILE_INTERVAL_MS = 60_000;
const RETRY_STEPS_MS = [5_000, 15_000, 30_000, 60_000, 120_000];

if (!DISCORD_TOKEN) {
  throw new Error('STAYVC_DISCORD_TOKEN is required');
}

const commands = [
  new SlashCommandBuilder()
    .setName('vcjoin')
    .setDescription('현재 들어가 있는 음성 채널에 상주합니다.'),
  new SlashCommandBuilder()
    .setName('vcleave')
    .setDescription('음성 채널에서 나가고 상주를 해제합니다.'),
  new SlashCommandBuilder()
    .setName('vcstatus')
    .setDescription('현재 상주 상태와 음성 연결 상태를 확인합니다.'),
  new SlashCommandBuilder()
    .setName('vcrejoin')
    .setDescription('저장된 상주 채널로 강제 재접속을 시도합니다.'),
].map((command) => command.toJSON());

function ensureConfigDir() {
  fs.mkdirSync(path.dirname(CONFIG_FILE), { recursive: true });
}

function loadConfig() {
  ensureConfigDir();
  try {
    const raw = fs.readFileSync(CONFIG_FILE, 'utf8');
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (error) {
    return {};
  }
}

function saveConfig(config) {
  ensureConfigDir();
  fs.writeFileSync(CONFIG_FILE, JSON.stringify(config, null, 2));
}

const config = loadConfig();
const reconnectState = new Map();
const observedConnections = new WeakSet();

const client = new Client({
  intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates],
});

function log(message) {
  console.log(`[STAYVC] ${message}`);
}

function getGuildConfig(guildId) {
  const key = String(guildId);
  if (!config[key] || typeof config[key] !== 'object') {
    config[key] = {};
  }
  return config[key];
}

function getTargetChannelId(guildId) {
  return getGuildConfig(guildId).targetChannelId || null;
}

function setTargetChannelId(guildId, channelId) {
  const guildConfig = getGuildConfig(guildId);
  if (guildConfig.targetChannelId === channelId) {
    return;
  }
  guildConfig.targetChannelId = channelId;
  guildConfig.updatedAt = new Date().toISOString();
  saveConfig(config);
}

function clearTargetChannelId(guildId) {
  const guildConfig = getGuildConfig(guildId);
  if (!guildConfig.targetChannelId) {
    return;
  }
  delete guildConfig.targetChannelId;
  guildConfig.updatedAt = new Date().toISOString();
  saveConfig(config);
}

function getRetryDelay(attempt) {
  return RETRY_STEPS_MS[Math.min(attempt - 1, RETRY_STEPS_MS.length - 1)];
}

function clearReconnectTimer(guildId) {
  const state = reconnectState.get(String(guildId));
  if (!state) {
    return;
  }
  if (state.timer) {
    clearTimeout(state.timer);
  }
  reconnectState.delete(String(guildId));
}

function scheduleReconnect(guildId, reason) {
  const key = String(guildId);
  const targetChannelId = getTargetChannelId(key);
  if (!targetChannelId) {
    return;
  }

  const previous = reconnectState.get(key);
  if (previous && previous.timer) {
    return;
  }

  const attempt = (previous?.attempt || 0) + 1;
  const delayMs = getRetryDelay(attempt);

  log(`guild=${key} reconnect scheduled in ${delayMs}ms (attempt=${attempt}, reason=${reason})`);

  const timer = setTimeout(async () => {
    reconnectState.set(key, { attempt, timer: null });
    await restoreVoiceForGuild(key, `scheduled:${reason}`);
  }, delayMs);

  reconnectState.set(key, { attempt, timer });
}

async function fetchVoiceChannel(guild, channelId) {
  let channel = guild.channels.cache.get(channelId);
  if (!channel) {
    channel = await guild.channels.fetch(channelId).catch(() => null);
  }
  if (!channel) {
    return null;
  }
  if (channel.type !== ChannelType.GuildVoice && channel.type !== ChannelType.GuildStageVoice) {
    return null;
  }
  return channel;
}

function observeConnection(connection, guildId) {
  if (observedConnections.has(connection)) {
    return;
  }
  observedConnections.add(connection);

  connection.on('stateChange', (oldState, newState) => {
    log(
      `guild=${guildId} voice state ${oldState.status} -> ${newState.status}` +
        ` channel=${connection.joinConfig.channelId ?? 'none'}`
    );
  });

  connection.on(VoiceConnectionStatus.Ready, () => {
    clearReconnectTimer(guildId);
    log(`guild=${guildId} voice ready channel=${connection.joinConfig.channelId}`);
  });

  connection.on(VoiceConnectionStatus.Disconnected, async () => {
    if (!getTargetChannelId(guildId)) {
      return;
    }

    try {
      await Promise.race([
        entersState(connection, VoiceConnectionStatus.Signalling, TRANSIENT_DISCONNECT_GRACE_MS),
        entersState(connection, VoiceConnectionStatus.Connecting, TRANSIENT_DISCONNECT_GRACE_MS),
      ]);
      log(`guild=${guildId} transient disconnect detected, library is recovering`);
    } catch (error) {
      log(`guild=${guildId} non-transient disconnect, scheduling reconnect`);
      try {
        connection.destroy();
      } catch (destroyError) {
        // Ignore destroy races; reconnect scheduler will handle recovery.
      }
      scheduleReconnect(guildId, 'disconnected');
    }
  });

  connection.on(VoiceConnectionStatus.Destroyed, () => {
    if (getTargetChannelId(guildId)) {
      scheduleReconnect(guildId, 'destroyed');
    }
  });

  connection.on('error', (error) => {
    log(`guild=${guildId} voice error: ${error.message}`);
  });
}

async function connectToChannel(channel, reason) {
  const guildId = String(channel.guild.id);
  const existing = getVoiceConnection(guildId);

  if (existing && existing.joinConfig.channelId === channel.id) {
    observeConnection(existing, guildId);
    try {
      await entersState(existing, VoiceConnectionStatus.Ready, 10_000);
      clearReconnectTimer(guildId);
      return existing;
    } catch (error) {
      try {
        existing.rejoin();
      } catch (rejoinError) {
        // Fall through to a fresh join below.
      }
    }
  }

  if (existing && existing.joinConfig.channelId !== channel.id) {
    try {
      existing.destroy();
    } catch (error) {
      // Ignore; a fresh connection will be created below.
    }
  }

  log(`guild=${guildId} joining channel=${channel.id} (${channel.name}) reason=${reason}`);

  const connection = joinVoiceChannel({
    channelId: channel.id,
    guildId,
    adapterCreator: channel.guild.voiceAdapterCreator,
    selfDeaf: true,
    selfMute: false,
  });

  observeConnection(connection, guildId);

  try {
    await entersState(connection, VoiceConnectionStatus.Ready, READY_TIMEOUT_MS);
    clearReconnectTimer(guildId);
    return connection;
  } catch (error) {
    log(`guild=${guildId} failed to reach READY within ${READY_TIMEOUT_MS}ms`);
    try {
      connection.destroy();
    } catch (destroyError) {
      // Ignore cleanup races.
    }
    scheduleReconnect(guildId, 'ready-timeout');
    return null;
  }
}

async function restoreVoiceForGuild(guildId, reason) {
  const targetChannelId = getTargetChannelId(guildId);
  if (!targetChannelId) {
    return null;
  }

  const guild =
    client.guilds.cache.get(guildId) || (await client.guilds.fetch(guildId).catch(() => null));
  if (!guild) {
    log(`guild=${guildId} restore skipped: guild unavailable`);
    return null;
  }

  const channel = await fetchVoiceChannel(guild, targetChannelId);
  if (!channel) {
    log(`guild=${guildId} restore skipped: target channel unavailable (${targetChannelId})`);
    return null;
  }

  return connectToChannel(channel, reason);
}

async function registerCommands() {
  const rest = new REST({ version: '10' }).setToken(DISCORD_TOKEN);
  for (const guild of client.guilds.cache.values()) {
    try {
      await rest.put(Routes.applicationGuildCommands(client.user.id, guild.id), {
        body: commands,
      });
      log(`registered commands for guild=${guild.id} (${guild.name})`);
    } catch (error) {
      log(`failed to register commands for guild=${guild.id}: ${error.message}`);
    }
  }
}

function getStatusSummary(guildId) {
  const targetChannelId = getTargetChannelId(guildId);
  const connection = getVoiceConnection(String(guildId));
  const retry = reconnectState.get(String(guildId));
  return {
    targetChannelId,
    currentChannelId: connection?.joinConfig?.channelId || null,
    status: connection?.state?.status || 'none',
    retryAttempt: retry?.attempt || 0,
    retryScheduled: Boolean(retry?.timer),
  };
}

client.once(Events.ClientReady, async (readyClient) => {
  log(`ready as ${readyClient.user.tag}`);
  await registerCommands();

  setTimeout(() => {
    for (const guild of readyClient.guilds.cache.values()) {
      const targetChannelId = getTargetChannelId(guild.id);
      if (targetChannelId) {
        void restoreVoiceForGuild(guild.id, 'startup');
      }
    }
  }, STARTUP_RESTORE_DELAY_MS);

  setInterval(() => {
    for (const guild of readyClient.guilds.cache.values()) {
      const targetChannelId = getTargetChannelId(guild.id);
      if (!targetChannelId) {
        continue;
      }
      const connection = getVoiceConnection(guild.id);
      if (!connection) {
        scheduleReconnect(guild.id, 'reconcile');
      }
    }
  }, RECONCILE_INTERVAL_MS);
});

client.on(Events.GuildCreate, async (guild) => {
  try {
    const rest = new REST({ version: '10' }).setToken(DISCORD_TOKEN);
    await rest.put(Routes.applicationGuildCommands(client.user.id, guild.id), {
      body: commands,
    });
    log(`registered commands for new guild=${guild.id} (${guild.name})`);
  } catch (error) {
    log(`failed to register commands for new guild=${guild.id}: ${error.message}`);
  }
});

client.on(Events.VoiceStateUpdate, (oldState, newState) => {
  if (newState.id !== client.user?.id) {
    return;
  }

  if (newState.channelId) {
    if (getTargetChannelId(newState.guild.id) !== newState.channelId) {
      setTargetChannelId(newState.guild.id, newState.channelId);
      log(`guild=${newState.guild.id} target updated to moved channel=${newState.channelId}`);
    }
  }
});

client.on(Events.InteractionCreate, async (interaction) => {
  if (!interaction.isChatInputCommand() || !interaction.guild) {
    return;
  }

  if (interaction.commandName === 'vcjoin') {
    const member = await interaction.guild.members.fetch(interaction.user.id).catch(() => null);
    const channel = member?.voice?.channel || null;
    if (!channel) {
      await interaction.reply({ content: '음성 채널에 먼저 들어가 주세요.', ephemeral: true });
      return;
    }

    setTargetChannelId(interaction.guild.id, channel.id);
    const connection = await connectToChannel(channel, 'slash:vcjoin');
    if (!connection) {
      await interaction.reply({
        content: '연결은 시도했지만 READY 상태 확인에 실패했습니다. 백그라운드 재시도를 계속합니다.',
        ephemeral: true,
      });
      return;
    }

    await interaction.reply({
      content: `상주 시작: ${channel.toString()}`,
      ephemeral: true,
    });
    return;
  }

  if (interaction.commandName === 'vcleave') {
    clearTargetChannelId(interaction.guild.id);
    clearReconnectTimer(interaction.guild.id);
    const connection = getVoiceConnection(String(interaction.guild.id));
    if (connection) {
      try {
        connection.destroy();
      } catch (error) {
        // Ignore destroy races.
      }
    }
    await interaction.reply({ content: '상주를 해제하고 음성 채널에서 나갔습니다.', ephemeral: true });
    return;
  }

  if (interaction.commandName === 'vcrejoin') {
    const targetChannelId = getTargetChannelId(interaction.guild.id);
    if (!targetChannelId) {
      await interaction.reply({ content: '저장된 상주 채널이 없습니다.', ephemeral: true });
      return;
    }

    clearReconnectTimer(interaction.guild.id);
    const connection = getVoiceConnection(String(interaction.guild.id));
    if (connection) {
      try {
        connection.destroy();
      } catch (error) {
        // Ignore destroy races.
      }
    }

    const restored = await restoreVoiceForGuild(interaction.guild.id, 'slash:vcrejoin');
    await interaction.reply({
      content: restored ? '저장된 채널로 다시 붙었습니다.' : '재접속을 시작했습니다. 실패하면 백그라운드 재시도를 계속합니다.',
      ephemeral: true,
    });
    return;
  }

  if (interaction.commandName === 'vcstatus') {
    const status = getStatusSummary(interaction.guild.id);
    await interaction.reply({
      content:
        `targetChannelId: ${status.targetChannelId || 'none'}\n` +
        `currentChannelId: ${status.currentChannelId || 'none'}\n` +
        `voiceStatus: ${status.status}\n` +
        `retryAttempt: ${status.retryAttempt}\n` +
        `retryScheduled: ${status.retryScheduled}`,
      ephemeral: true,
    });
  }
});

client.on(Events.Error, (error) => {
  log(`client error: ${error.message}`);
});

process.on('unhandledRejection', (error) => {
  console.error('[STAYVC] unhandledRejection', error);
});

process.on('uncaughtException', (error) => {
  console.error('[STAYVC] uncaughtException', error);
});

client.login(DISCORD_TOKEN);
