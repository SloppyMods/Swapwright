using Swapwright.Core;

namespace Swapwright.Plugin;

/// <summary>Builds the swapd HTTP client for this machine: 127.0.0.1 only, token read from the Wine Z: mapping.</summary>
public static class SwapdClient
{
    /// <summary>Wine maps Z:\ to / (CONTRACT.md Transport).</summary>
    public const string TokenPath = @"Z:\home\SloppyMods\.local\share\swapwright\token";

    public static SwapdApi Create(int port) =>
        new(new Uri($"http://127.0.0.1:{port}"), ReadToken);

    /// <summary>Called at startup and again after a 401. Read-only.</summary>
    public static string? ReadToken() =>
        File.Exists(TokenPath) ? File.ReadAllText(TokenPath).Trim() : null;
}
