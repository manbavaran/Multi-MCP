// Assets/Editor/UnityMcpBridge.cs  (v2.1 — Thread-safe, Domain-Reload-safe)
// Multi-MCP compatible Unity Editor bridge (HTTP + MCP JSON-RPC 2.0)
//
// FIXES in v2.1:
//  * ThreadAbortException handled gracefully — no more "Thread was being aborted" warnings
//  * BeginGetContext (async I/O) replaces blocking GetContext — server thread never blocks
//  * AssemblyReloadEvents: server auto-stops before domain reload, auto-restarts after
//  * RunOnMainThread uses ManualResetEventSlim instead of Thread.Sleep polling
//  * StopServer is idempotent and safe to call from any thread
//
// Tools (12):
//  unity.manage_gameobject, unity.manage_scene, unity.manage_components,
//  unity.get_component_property, unity.set_component_property,
//  unity.call_component_method, unity.send_event, unity.control_playmode,
//  unity.query_scene, unity.manage_asset, unity.execute_menu_item,
//  unity.read_console
//
// MCP endpoint: POST /mcp  (JSON-RPC 2.0)
// Health:       GET  /health
// Browser info: GET  /mcp
// Legacy:       GET  /tools/list  |  POST /tools/call
//
// Dependencies:
//   com.unity.nuget.newtonsoft-json  (Package Manager → Add by name)

using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

[InitializeOnLoad]
public static class UnityMcpBridge
{
    // ======== Config ========
    private static int Port = 23457;
    private static string AuthToken = "";
    private static bool AutoStart = true;  // default: auto-start on Editor load

    // ======== Server State ========
    private static HttpListener _listener;
    private static volatile bool _running;
    private static readonly object _lock = new object();

    /// <summary>True if the HTTP server is currently accepting requests.</summary>
    public static bool IsRunning => _running;

    // ======== Static Constructor (runs on every domain reload) ========

    static UnityMcpBridge()
    {
        Port      = EditorPrefs.GetInt   ("MultiMCP.Unity.Port",      23457);
        AuthToken = EditorPrefs.GetString("MultiMCP.Unity.Token",     "");
        AutoStart = EditorPrefs.GetBool  ("MultiMCP.Unity.AutoStart", true);  // default true

        // Stop cleanly before the next domain reload
        AssemblyReloadEvents.beforeAssemblyReload += OnBeforeAssemblyReload;
        // Optionally restart after reload
        AssemblyReloadEvents.afterAssemblyReload  += OnAfterAssemblyReload;

        if (AutoStart)
        {
            StartServer();
        }
        else
        {
            // Remind the user to start the server manually
            Debug.Log("[UnityMcpBridge] v2.1 loaded. AutoStart is disabled — server is NOT running.\n" +
                      "  ▶  To start now:    Multi-MCP  →  Unity Bridge  →  Start\n" +
                      "  ▶  To enable auto:  Multi-MCP  →  Unity Bridge  →  Settings  →  enable Auto Start on Editor Load");
        }
    }

    private static void OnBeforeAssemblyReload()
    {
        if (_running)
        {
            Debug.Log("[UnityMcpBridge] Domain reload detected — stopping server.");
            StopServer();
        }
    }

    private static void OnAfterAssemblyReload()
    {
        if (AutoStart && !_running)
        {
            Debug.Log("[UnityMcpBridge] Domain reload complete — restarting server.");
            StartServer();
        }
    }

    // ======== Menu Items ========

    [MenuItem("Multi-MCP/Unity Bridge/Start")]
    public static void StartServer()
    {
        lock (_lock)
        {
            if (_running)
            {
                Debug.Log("[UnityMcpBridge] Already running.");
                return;
            }

            var prefix = $"http://127.0.0.1:{Port}/";
            _listener = new HttpListener();
            _listener.Prefixes.Add(prefix);

            try { _listener.Start(); }
            catch (Exception e)
            {
                Debug.LogError($"[UnityMcpBridge] Failed to start on port {Port}: {e.Message}");
                _listener = null;
                return;
            }

            _running = true;
        }

        // Kick off the first async accept — no dedicated thread needed
        BeginAccept();

        Debug.Log($"[UnityMcpBridge] v2.1 started.\n" +
                  $"  MCP endpoint : POST http://127.0.0.1:{Port}/mcp\n" +
                  $"  Health check : GET  http://127.0.0.1:{Port}/health\n" +
                  $"  Register in Multi-MCP GUI: transport=http  endpoint=http://127.0.0.1:{Port}/mcp");
    }

    [MenuItem("Multi-MCP/Unity Bridge/Stop")]
    public static void StopServer()
    {
        lock (_lock)
        {
            if (!_running) return;
            _running = false;
            try { _listener?.Stop(); }  catch { }
            try { _listener?.Close(); } catch { }
            _listener = null;
        }
        Debug.Log("[UnityMcpBridge] Stopped.");
    }

    [MenuItem("Multi-MCP/Unity Bridge/Settings")]
    public static void Settings() => UnityMcpBridgeSettingsWindow.ShowWindow();

    [MenuItem("Multi-MCP/Unity Bridge/Status")]
    public static void PrintStatus()
    {
        Debug.Log($"[UnityMcpBridge] Running={_running}  Port={Port}  " +
                  $"Auth={(!string.IsNullOrEmpty(AuthToken) ? "enabled" : "disabled")}");
    }

    // ======== Async Accept Loop (no blocking thread) ========

    private static void BeginAccept()
    {
        HttpListener listener;
        lock (_lock) { listener = _listener; }
        if (!_running || listener == null) return;

        try
        {
            listener.BeginGetContext(OnContext, listener);
        }
        catch (ObjectDisposedException) { /* listener was closed */ }
        catch (HttpListenerException) { /* listener was stopped */ }
        catch (Exception e)
        {
            if (_running)
                Debug.LogWarning($"[UnityMcpBridge] BeginGetContext error: {e.Message}");
        }
    }

    private static void OnContext(IAsyncResult ar)
    {
        var listener = (HttpListener)ar.AsyncState;

        // Re-arm immediately so the next request can be accepted in parallel
        if (_running)
            BeginAccept();

        HttpListenerContext ctx = null;
        try
        {
            ctx = listener.EndGetContext(ar);
        }
        catch (ObjectDisposedException) { return; }
        catch (HttpListenerException) { return; }
        catch (Exception e)
        {
            if (_running)
                Debug.LogWarning($"[UnityMcpBridge] EndGetContext error: {e.Message}");
            return;
        }

        try
        {
            HandleRequest(ctx);
        }
        catch (Exception e)
        {
            Debug.LogWarning($"[UnityMcpBridge] HandleRequest error: {e.Message}");
            try
            {
                ctx.Response.StatusCode = 500;
                ctx.Response.Close();
            }
            catch { }
        }
    }

    // ======== Request Handler ========

    private static void HandleRequest(HttpListenerContext ctx)
    {
        var req = ctx.Request;
        var res = ctx.Response;

        // CORS
        res.Headers.Add("Access-Control-Allow-Origin",  "http://localhost:8765");
        res.Headers.Add("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
        res.Headers.Add("Access-Control-Allow-Headers", "Content-Type, Authorization");

        if (req.HttpMethod == "OPTIONS")
        {
            res.StatusCode = 204;
            res.Close();
            return;
        }

        Debug.Log($"[UnityMcpBridge] {req.HttpMethod} {req.Url.AbsolutePath}");

        // Auth
        if (!string.IsNullOrEmpty(AuthToken))
        {
            var auth = req.Headers["Authorization"] ?? "";
            if (auth != $"Bearer {AuthToken}")
            {
                WriteJson(res, 401, "{\"error\":\"unauthorized\"}");
                return;
            }
        }

        var method = req.HttpMethod;
        var path   = req.Url.AbsolutePath;

        // ── MCP JSON-RPC 2.0 ─────────────────────────────────────────────
        if (method == "POST" && path == "/mcp")
        {
            WriteJson(res, 200, HandleJsonRpc(ReadBody(req)));
            return;
        }

        // ── Browser-friendly GET /mcp ─────────────────────────────────────
        if (method == "GET" && path == "/mcp")
        {
            WriteJson(res, 200,
                $"{{\"status\":\"ok\",\"service\":\"unity-mcp-bridge\",\"version\":\"2.1\"," +
                $"\"note\":\"POST required for JSON-RPC 2.0\"," +
                $"\"endpoint\":\"POST http://127.0.0.1:{Port}/mcp\"," +
                $"\"health\":\"GET http://127.0.0.1:{Port}/health\"}}");
            return;
        }

        // ── Health check ──────────────────────────────────────────────────
        if (method == "GET" && path == "/health")
        {
            WriteJson(res, 200,
                $"{{\"status\":\"ok\",\"service\":\"unity-mcp-bridge\",\"version\":\"2.1\",\"port\":{Port}}}");
            return;
        }

        // ── Legacy endpoints ──────────────────────────────────────────────
        if (method == "GET" && path == "/tools/list")
        {
            WriteJson(res, 200, ToolsListLegacy());
            return;
        }

        if (method == "POST" && path == "/tools/call")
        {
            WriteJson(res, 200, HandleLegacyToolCall(ReadBody(req)));
            return;
        }

        WriteJson(res, 404, "{\"error\":\"not_found\"}");
    }

    // ======== MCP JSON-RPC 2.0 ========

    private static string HandleJsonRpc(string body)
    {
        JObject req;
        try { req = JObject.Parse(body); }
        catch { return "{\"jsonrpc\":\"2.0\",\"id\":1,\"error\":{\"code\":-32700,\"message\":\"parse error\"}}"; }

        var idTok = req["id"] ?? 1;
        string idRaw = (idTok.Type == JTokenType.String)
            ? $"\"{Escape((string)idTok)}\""
            : idTok.ToString(Formatting.None);

        var method = (string)req["method"];
        if (string.IsNullOrEmpty(method))
            return $"{{\"jsonrpc\":\"2.0\",\"id\":{idRaw},\"error\":{{\"code\":-32600,\"message\":\"invalid request\"}}}}";

        if (method == "tools/list")
            return $"{{\"jsonrpc\":\"2.0\",\"id\":{idRaw},\"result\":{ToolsListJsonRpcResult()}}}";

        if (method == "tools/call")
        {
            var p    = (JObject)(req["params"] ?? new JObject());
            var name = (string)p["name"];
            var args = (JObject)(p["arguments"] ?? new JObject());

            if (string.IsNullOrEmpty(name))
                return $"{{\"jsonrpc\":\"2.0\",\"id\":{idRaw},\"error\":{{\"code\":-32602,\"message\":\"missing tool name\"}}}}";

            string resultJson = DispatchToolCall(name, args);
            return $"{{\"jsonrpc\":\"2.0\",\"id\":{idRaw},\"result\":{resultJson}}}";
        }

        return $"{{\"jsonrpc\":\"2.0\",\"id\":{idRaw},\"error\":{{\"code\":-32601,\"message\":\"method not found\"}}}}";
    }

    // ======== Tools List (JSON-RPC) ========

    private static string ToolsListJsonRpcResult()
    {
        return @"
{
  ""tools"": [
    {
      ""name"": ""unity.manage_gameobject"",
      ""description"": ""Create/find/delete/update GameObjects in the active scene (action-based)"",
      ""inputSchema"": {
        ""type"": ""object"",
        ""properties"": {
          ""action"": { ""type"": ""string"", ""enum"": [""find"",""get"",""create"",""delete"",""set_active"",""set_transform""] },
          ""path"": { ""type"": ""string"", ""description"": ""Hierarchy path like Root/Child"" },
          ""name"": { ""type"": ""string"" },
          ""parent_path"": { ""type"": ""string"" },
          ""position"": { ""type"": ""array"", ""items"": {""type"":""number""}, ""minItems"":3, ""maxItems"":3 },
          ""rotation"": { ""type"": ""array"", ""items"": {""type"":""number""}, ""minItems"":3, ""maxItems"":3 },
          ""scale"":    { ""type"": ""array"", ""items"": {""type"":""number""}, ""minItems"":3, ""maxItems"":3 },
          ""active"":   { ""type"": ""boolean"" },
          ""query"":    { ""type"": ""string"", ""description"": ""Substring match on GameObject name"" }
        },
        ""required"": [""action""]
      }
    },
    {
      ""name"": ""unity.manage_scene"",
      ""description"": ""Open/save/list scenes"",
      ""inputSchema"": {
        ""type"": ""object"",
        ""properties"": {
          ""action"": { ""type"": ""string"", ""enum"": [""list_open"",""save_active"",""open""] },
          ""path"": { ""type"": ""string"" }
        },
        ""required"": [""action""]
      }
    },
    {
      ""name"": ""unity.manage_components"",
      ""description"": ""List/add/remove components on a GameObject"",
      ""inputSchema"": {
        ""type"": ""object"",
        ""properties"": {
          ""action"": { ""type"": ""string"", ""enum"": [""list"",""add"",""remove""] },
          ""path"": { ""type"": ""string"" },
          ""type"": { ""type"": ""string"", ""description"": ""Component type e.g. UnityEngine.BoxCollider"" }
        },
        ""required"": [""action"",""path""]
      }
    },
    {
      ""name"": ""unity.get_component_property"",
      ""description"": ""Read a field or property value from a component on a GameObject. Use this to observe game state (HP, score, flags, etc.)"",
      ""inputSchema"": {
        ""type"": ""object"",
        ""properties"": {
          ""path"":      { ""type"": ""string"", ""description"": ""Hierarchy path of the GameObject"" },
          ""component"": { ""type"": ""string"", ""description"": ""Component type name (e.g. PlayerController)"" },
          ""property"":  { ""type"": ""string"", ""description"": ""Field or property name (e.g. health, moveSpeed)"" }
        },
        ""required"": [""path"",""component"",""property""]
      }
    },
    {
      ""name"": ""unity.set_component_property"",
      ""description"": ""Write a value to a field or property on a component. Use to modify game state."",
      ""inputSchema"": {
        ""type"": ""object"",
        ""properties"": {
          ""path"":      { ""type"": ""string"" },
          ""component"": { ""type"": ""string"" },
          ""property"":  { ""type"": ""string"" },
          ""value"":     { ""description"": ""New value (string/number/boolean/array)"" }
        },
        ""required"": [""path"",""component"",""property"",""value""]
      }
    },
    {
      ""name"": ""unity.call_component_method"",
      ""description"": ""Invoke a public method on a component. Use to trigger game actions (Attack, OpenDoor, AddItem, etc.)"",
      ""inputSchema"": {
        ""type"": ""object"",
        ""properties"": {
          ""path"":      { ""type"": ""string"" },
          ""component"": { ""type"": ""string"" },
          ""method"":    { ""type"": ""string"", ""description"": ""Method name (e.g. Attack, Heal, AddItem)"" },
          ""args"":      { ""type"": ""array"", ""description"": ""Positional arguments"", ""items"": {} }
        },
        ""required"": [""path"",""component"",""method""]
      }
    },
    {
      ""name"": ""unity.send_event"",
      ""description"": ""Send a named message to a component via SendMessage. Useful for event-driven behaviours."",
      ""inputSchema"": {
        ""type"": ""object"",
        ""properties"": {
          ""path"":    { ""type"": ""string"" },
          ""message"": { ""type"": ""string"", ""description"": ""Method name to broadcast (e.g. OnPlayerDied)"" },
          ""arg"":     { ""description"": ""Optional single argument"" }
        },
        ""required"": [""path"",""message""]
      }
    },
    {
      ""name"": ""unity.control_playmode"",
      ""description"": ""Control Unity Editor play mode and time scale."",
      ""inputSchema"": {
        ""type"": ""object"",
        ""properties"": {
          ""action"":    { ""type"": ""string"", ""enum"": [""enter"",""exit"",""pause"",""resume"",""step"",""get_state"",""set_timescale""] },
          ""timescale"": { ""type"": ""number"", ""minimum"": 0, ""maximum"": 100 }
        },
        ""required"": [""action""]
      }
    },
    {
      ""name"": ""unity.query_scene"",
      ""description"": ""Return a structured snapshot of the active scene hierarchy. Use as an observation step before planning actions."",
      ""inputSchema"": {
        ""type"": ""object"",
        ""properties"": {
          ""filter_tag"":       { ""type"": ""string"" },
          ""filter_component"": { ""type"": ""string"" },
          ""filter_name"":      { ""type"": ""string"" },
          ""max_depth"":        { ""type"": ""integer"", ""minimum"": 1, ""maximum"": 10 },
          ""include_inactive"": { ""type"": ""boolean"" }
        }
      }
    },
    {
      ""name"": ""unity.manage_asset"",
      ""description"": ""Find assets in the AssetDatabase and instantiate prefabs into the scene."",
      ""inputSchema"": {
        ""type"": ""object"",
        ""properties"": {
          ""action"":      { ""type"": ""string"", ""enum"": [""find"",""instantiate""] },
          ""filter"":      { ""type"": ""string"" },
          ""asset_path"":  { ""type"": ""string"" },
          ""position"":    { ""type"": ""array"", ""items"": {""type"":""number""}, ""minItems"":3, ""maxItems"":3 },
          ""parent_path"": { ""type"": ""string"" }
        },
        ""required"": [""action""]
      }
    },
    {
      ""name"": ""unity.execute_menu_item"",
      ""description"": ""Execute a Unity Editor menu item by path (e.g. File/Save Project)"",
      ""inputSchema"": {
        ""type"": ""object"",
        ""properties"": { ""menu"": { ""type"": ""string"" } },
        ""required"": [""menu""]
      }
    },
    {
      ""name"": ""unity.read_console"",
      ""description"": ""Tail the Unity Editor log. Use after actions to check for errors or confirm success."",
      ""inputSchema"": {
        ""type"": ""object"",
        ""properties"": {
          ""max_lines"": { ""type"": ""integer"", ""minimum"": 1, ""maximum"": 2000 },
          ""filter"":    { ""type"": ""string"" }
        }
      }
    }
  ]
}
".Trim();
    }

    // ======== Legacy ========

    private static string ToolsListLegacy()
    {
        return "{\"tools\":[" +
            "{\"name\":\"unity.manage_gameobject\"}," +
            "{\"name\":\"unity.manage_scene\"}," +
            "{\"name\":\"unity.manage_components\"}," +
            "{\"name\":\"unity.get_component_property\"}," +
            "{\"name\":\"unity.set_component_property\"}," +
            "{\"name\":\"unity.call_component_method\"}," +
            "{\"name\":\"unity.send_event\"}," +
            "{\"name\":\"unity.control_playmode\"}," +
            "{\"name\":\"unity.query_scene\"}," +
            "{\"name\":\"unity.manage_asset\"}," +
            "{\"name\":\"unity.execute_menu_item\"}," +
            "{\"name\":\"unity.read_console\"}" +
            "]}";
    }

    private static string HandleLegacyToolCall(string body)
    {
        JObject req;
        try { req = JObject.Parse(body); }
        catch { return "{\"ok\":false,\"error\":\"parse_error\"}"; }
        string name = (string)req["name"];
        var args = (JObject)(req["arguments"] ?? new JObject());
        return DispatchToolCall(name, args);
    }

    // ======== Tool Dispatch ========

    private static string DispatchToolCall(string name, JObject args)
    {
        switch (name)
        {
            case "unity.manage_gameobject":      return RunOnMainThread(() => ManageGameObject(args));
            case "unity.manage_scene":           return RunOnMainThread(() => ManageScene(args));
            case "unity.manage_components":      return RunOnMainThread(() => ManageComponents(args));
            case "unity.get_component_property": return RunOnMainThread(() => GetComponentProperty(args));
            case "unity.set_component_property": return RunOnMainThread(() => SetComponentProperty(args));
            case "unity.call_component_method":  return RunOnMainThread(() => CallComponentMethod(args));
            case "unity.send_event":             return RunOnMainThread(() => SendEvent(args));
            case "unity.control_playmode":       return RunOnMainThread(() => ControlPlayMode(args));
            case "unity.query_scene":            return RunOnMainThread(() => QueryScene(args));
            case "unity.manage_asset":           return RunOnMainThread(() => ManageAsset(args));
            case "unity.execute_menu_item":      return RunOnMainThread(() => ExecuteMenuItem(args));
            case "unity.read_console":           return RunOnMainThread(() => ReadConsole(args));
            default:
                return $"{{\"ok\":false,\"error\":\"unknown_tool\",\"tool\":\"{Escape(name)}\"}}";
        }
    }

    // ======== Tool Implementations ========

    private static string ManageGameObject(JObject a)
    {
        var action = (string)a["action"];

        if (action == "find")
        {
            string query = (string)a["query"] ?? "";
            var roots = SceneManager.GetActiveScene().GetRootGameObjects();
            var sb = new StringBuilder();
            sb.Append("{\"ok\":true,\"result\":{\"objects\":[");
            bool first = true;
            foreach (var root in roots)
                FindGameObjects(root, query, sb, ref first);
            sb.Append("]}}");
            return sb.ToString();
        }

        if (action == "get")
        {
            string path = (string)a["path"];
            if (string.IsNullOrEmpty(path)) return "{\"ok\":false,\"error\":\"missing_path\"}";
            var go = FindByHierarchyPath(path);
            if (go == null) return "{\"ok\":false,\"error\":\"not_found\"}";
            var t = go.transform;
            return "{\"ok\":true,\"result\":{" +
                $"\"path\":\"{Escape(GetHierarchyPath(go))}\"," +
                $"\"name\":\"{Escape(go.name)}\"," +
                $"\"active\":{(go.activeSelf ? "true" : "false")}," +
                $"\"position\":[{t.position.x:F3},{t.position.y:F3},{t.position.z:F3}]," +
                $"\"rotation\":[{t.eulerAngles.x:F3},{t.eulerAngles.y:F3},{t.eulerAngles.z:F3}]," +
                $"\"scale\":[{t.localScale.x:F3},{t.localScale.y:F3},{t.localScale.z:F3}]" +
                "}}";
        }

        if (action == "create")
        {
            string name = (string)a["name"] ?? "NewGameObject";
            string parentPath = (string)a["parent_path"] ?? "";
            GameObject go = new GameObject(name);
            if (!string.IsNullOrEmpty(parentPath))
            {
                var parent = FindByHierarchyPath(parentPath);
                if (parent != null) go.transform.SetParent(parent.transform, false);
            }
            Undo.RegisterCreatedObjectUndo(go, "Create GameObject");
            EditorSceneManager.MarkSceneDirty(SceneManager.GetActiveScene());
            return $"{{\"ok\":true,\"result\":{{\"path\":\"{Escape(GetHierarchyPath(go))}\"}}}}";
        }

        if (action == "delete")
        {
            string path = (string)a["path"];
            if (string.IsNullOrEmpty(path)) return "{\"ok\":false,\"error\":\"missing_path\"}";
            var go = FindByHierarchyPath(path);
            if (go == null) return "{\"ok\":false,\"error\":\"not_found\"}";
            Undo.DestroyObjectImmediate(go);
            EditorSceneManager.MarkSceneDirty(SceneManager.GetActiveScene());
            return "{\"ok\":true,\"result\":{\"deleted\":true}}";
        }

        if (action == "set_active")
        {
            string path = (string)a["path"];
            if (string.IsNullOrEmpty(path)) return "{\"ok\":false,\"error\":\"missing_path\"}";
            bool active = (bool?)a["active"] ?? true;
            var go = FindByHierarchyPath(path);
            if (go == null) return "{\"ok\":false,\"error\":\"not_found\"}";
            Undo.RecordObject(go, "Set Active");
            go.SetActive(active);
            EditorUtility.SetDirty(go);
            EditorSceneManager.MarkSceneDirty(SceneManager.GetActiveScene());
            return "{\"ok\":true,\"result\":{\"updated\":true}}";
        }

        if (action == "set_transform")
        {
            string path = (string)a["path"];
            if (string.IsNullOrEmpty(path)) return "{\"ok\":false,\"error\":\"missing_path\"}";
            var go = FindByHierarchyPath(path);
            if (go == null) return "{\"ok\":false,\"error\":\"not_found\"}";
            Undo.RecordObject(go.transform, "Set Transform");
            if (a["position"] is JArray p && p.Count >= 3)
                go.transform.position = new Vector3((float)p[0], (float)p[1], (float)p[2]);
            if (a["rotation"] is JArray r && r.Count >= 3)
                go.transform.eulerAngles = new Vector3((float)r[0], (float)r[1], (float)r[2]);
            if (a["scale"] is JArray s && s.Count >= 3)
                go.transform.localScale = new Vector3((float)s[0], (float)s[1], (float)s[2]);
            EditorUtility.SetDirty(go.transform);
            EditorSceneManager.MarkSceneDirty(SceneManager.GetActiveScene());
            return "{\"ok\":true,\"result\":{\"updated\":true}}";
        }

        return "{\"ok\":false,\"error\":\"unknown_action\"}";
    }

    private static void FindGameObjects(GameObject go, string query, StringBuilder sb, ref bool first)
    {
        if (string.IsNullOrEmpty(query) ||
            go.name.IndexOf(query, StringComparison.OrdinalIgnoreCase) >= 0)
        {
            if (!first) sb.Append(",");
            first = false;
            sb.Append($"{{\"path\":\"{Escape(GetHierarchyPath(go))}\",\"name\":\"{Escape(go.name)}\",\"active\":{(go.activeSelf ? "true" : "false")}}}");
        }
        for (int i = 0; i < go.transform.childCount; i++)
            FindGameObjects(go.transform.GetChild(i).gameObject, query, sb, ref first);
    }

    private static string ManageScene(JObject a)
    {
        var action = (string)a["action"];
        if (action == "list_open")
        {
            int n = SceneManager.sceneCount;
            var sb = new StringBuilder();
            sb.Append("{\"ok\":true,\"result\":{\"open_scenes\":[");
            for (int i = 0; i < n; i++)
            {
                var sc = SceneManager.GetSceneAt(i);
                sb.Append($"\"{Escape(sc.path)}\"");
                if (i < n - 1) sb.Append(",");
            }
            sb.Append("]}}");
            return sb.ToString();
        }
        if (action == "save_active")
        {
            var scene = SceneManager.GetActiveScene();
            if (!scene.isLoaded) return "{\"ok\":false,\"error\":\"no_active_scene\"}";
            bool saved = EditorSceneManager.SaveScene(scene);
            return $"{{\"ok\":true,\"result\":{{\"saved\":{(saved ? "true" : "false")}}}}}";
        }
        if (action == "open")
        {
            string path = (string)a["path"];
            if (string.IsNullOrEmpty(path)) return "{\"ok\":false,\"error\":\"missing_path\"}";
            var sc = EditorSceneManager.OpenScene(path, OpenSceneMode.Single);
            return $"{{\"ok\":true,\"result\":{{\"opened\":\"{Escape(sc.path)}\"}}}}";
        }
        return "{\"ok\":false,\"error\":\"unknown_action\"}";
    }

    private static string ManageComponents(JObject a)
    {
        var action = (string)a["action"];
        string path = (string)a["path"];
        if (string.IsNullOrEmpty(path)) return "{\"ok\":false,\"error\":\"missing_path\"}";
        var go = FindByHierarchyPath(path);
        if (go == null) return "{\"ok\":false,\"error\":\"not_found\"}";

        if (action == "list")
        {
            var comps = go.GetComponents<Component>();
            var sb = new StringBuilder();
            sb.Append("{\"ok\":true,\"result\":{\"components\":[");
            for (int i = 0; i < comps.Length; i++)
            {
                sb.Append($"\"{Escape(comps[i].GetType().FullName)}\"");
                if (i < comps.Length - 1) sb.Append(",");
            }
            sb.Append("]}}");
            return sb.ToString();
        }

        string typeName = (string)a["type"];
        if (string.IsNullOrEmpty(typeName)) return "{\"ok\":false,\"error\":\"missing_type\"}";
        var type = ResolveType(typeName);
        if (type == null) return "{\"ok\":false,\"error\":\"type_not_found\"}";

        if (action == "add")
        {
            Undo.AddComponent(go, type);
            EditorUtility.SetDirty(go);
            EditorSceneManager.MarkSceneDirty(SceneManager.GetActiveScene());
            return "{\"ok\":true,\"result\":{\"added\":true}}";
        }
        if (action == "remove")
        {
            var c = go.GetComponent(type);
            if (c == null) return "{\"ok\":false,\"error\":\"component_not_found\"}";
            Undo.DestroyObjectImmediate(c);
            EditorUtility.SetDirty(go);
            EditorSceneManager.MarkSceneDirty(SceneManager.GetActiveScene());
            return "{\"ok\":true,\"result\":{\"removed\":true}}";
        }
        return "{\"ok\":false,\"error\":\"unknown_action\"}";
    }

    private static string ExecuteMenuItem(JObject a)
    {
        string menu = (string)a["menu"];
        if (string.IsNullOrEmpty(menu)) return "{\"ok\":false,\"error\":\"missing_menu\"}";
        bool ok = EditorApplication.ExecuteMenuItem(menu);
        return $"{{\"ok\":true,\"result\":{{\"executed\":{(ok ? "true" : "false")}}}}}";
    }

    private static string ReadConsole(JObject a)
    {
        int maxLines = (int?)a["max_lines"] ?? 200;
        maxLines = Mathf.Clamp(maxLines, 1, 2000);
        string filter = (string)a["filter"] ?? "";

        string logPath = Application.consoleLogPath;
        if (string.IsNullOrEmpty(logPath) || !File.Exists(logPath))
            return "{\"ok\":false,\"error\":\"editor_log_not_found\"}";

        try
        {
            var lines = TailLines(logPath, maxLines * 3);
            if (!string.IsNullOrEmpty(filter))
            {
                var filtered = new List<string>();
                foreach (var l in lines)
                    if (l.IndexOf(filter, StringComparison.OrdinalIgnoreCase) >= 0)
                        filtered.Add(l);
                lines = filtered.ToArray();
                if (lines.Length > maxLines)
                {
                    var trimmed = new string[maxLines];
                    Array.Copy(lines, lines.Length - maxLines, trimmed, 0, maxLines);
                    lines = trimmed;
                }
            }

            var sb = new StringBuilder();
            sb.Append("{\"ok\":true,\"result\":{\"log_path\":\"");
            sb.Append(Escape(logPath));
            sb.Append("\",\"lines\":[");
            for (int i = 0; i < lines.Length; i++)
            {
                sb.Append($"\"{Escape(lines[i])}\"");
                if (i < lines.Length - 1) sb.Append(",");
            }
            sb.Append("]}}");
            return sb.ToString();
        }
        catch (Exception e)
        {
            return $"{{\"ok\":false,\"error\":\"exception\",\"message\":\"{Escape(e.Message)}\"}}";
        }
    }

    private static string GetComponentProperty(JObject a)
    {
        string path = (string)a["path"];
        string compName = (string)a["component"];
        string propName = (string)a["property"];

        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(compName) || string.IsNullOrEmpty(propName))
            return "{\"ok\":false,\"error\":\"missing_required_args\"}";

        var go = FindByHierarchyPath(path);
        if (go == null) return "{\"ok\":false,\"error\":\"gameobject_not_found\"}";

        var compType = ResolveType(compName);
        if (compType == null) return "{\"ok\":false,\"error\":\"component_type_not_found\"}";

        var comp = go.GetComponent(compType);
        if (comp == null) return "{\"ok\":false,\"error\":\"component_not_on_gameobject\"}";

        var field = compType.GetField(propName,
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
        if (field != null)
        {
            var val = field.GetValue(comp);
            return $"{{\"ok\":true,\"result\":{{\"value\":{SerializeValue(val)},\"type\":\"{Escape(field.FieldType.Name)}\"}}}}";
        }

        var prop = compType.GetProperty(propName, BindingFlags.Public | BindingFlags.Instance);
        if (prop != null && prop.CanRead)
        {
            var val = prop.GetValue(comp);
            return $"{{\"ok\":true,\"result\":{{\"value\":{SerializeValue(val)},\"type\":\"{Escape(prop.PropertyType.Name)}\"}}}}";
        }

        return $"{{\"ok\":false,\"error\":\"member_not_found\",\"member\":\"{Escape(propName)}\"}}";
    }

    private static string SetComponentProperty(JObject a)
    {
        string path = (string)a["path"];
        string compName = (string)a["component"];
        string propName = (string)a["property"];
        var value = a["value"];

        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(compName) ||
            string.IsNullOrEmpty(propName) || value == null)
            return "{\"ok\":false,\"error\":\"missing_required_args\"}";

        var go = FindByHierarchyPath(path);
        if (go == null) return "{\"ok\":false,\"error\":\"gameobject_not_found\"}";

        var compType = ResolveType(compName);
        if (compType == null) return "{\"ok\":false,\"error\":\"component_type_not_found\"}";

        var comp = go.GetComponent(compType);
        if (comp == null) return "{\"ok\":false,\"error\":\"component_not_on_gameobject\"}";

        Undo.RecordObject(comp, $"Set {compName}.{propName}");

        var field = compType.GetField(propName,
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
        if (field != null)
        {
            try
            {
                field.SetValue(comp, ConvertJToken(value, field.FieldType));
                EditorUtility.SetDirty(comp);
                EditorSceneManager.MarkSceneDirty(SceneManager.GetActiveScene());
                return "{\"ok\":true,\"result\":{\"updated\":true}}";
            }
            catch (Exception e)
            {
                return $"{{\"ok\":false,\"error\":\"set_failed\",\"message\":\"{Escape(e.Message)}\"}}";
            }
        }

        var prop = compType.GetProperty(propName, BindingFlags.Public | BindingFlags.Instance);
        if (prop != null && prop.CanWrite)
        {
            try
            {
                prop.SetValue(comp, ConvertJToken(value, prop.PropertyType));
                EditorUtility.SetDirty(comp);
                EditorSceneManager.MarkSceneDirty(SceneManager.GetActiveScene());
                return "{\"ok\":true,\"result\":{\"updated\":true}}";
            }
            catch (Exception e)
            {
                return $"{{\"ok\":false,\"error\":\"set_failed\",\"message\":\"{Escape(e.Message)}\"}}";
            }
        }

        return $"{{\"ok\":false,\"error\":\"member_not_found_or_readonly\",\"member\":\"{Escape(propName)}\"}}";
    }

    private static string CallComponentMethod(JObject a)
    {
        string path = (string)a["path"];
        string compName = (string)a["component"];
        string methodName = (string)a["method"];

        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(compName) || string.IsNullOrEmpty(methodName))
            return "{\"ok\":false,\"error\":\"missing_required_args\"}";

        var go = FindByHierarchyPath(path);
        if (go == null) return "{\"ok\":false,\"error\":\"gameobject_not_found\"}";

        var compType = ResolveType(compName);
        if (compType == null) return "{\"ok\":false,\"error\":\"component_type_not_found\"}";

        var comp = go.GetComponent(compType);
        if (comp == null) return "{\"ok\":false,\"error\":\"component_not_on_gameobject\"}";

        var argsToken = a["args"] as JArray;
        object[] methodArgs = null;

        // Find matching method overload
        var methods = compType.GetMethods(BindingFlags.Public | BindingFlags.Instance);
        MethodInfo target = null;
        foreach (var m in methods)
        {
            if (m.Name != methodName) continue;
            var mParams = m.GetParameters();
            if (argsToken == null && mParams.Length == 0) { target = m; break; }
            if (argsToken != null && mParams.Length == argsToken.Count)
            {
                target = m;
                methodArgs = new object[mParams.Length];
                for (int i = 0; i < mParams.Length; i++)
                    methodArgs[i] = ConvertJToken(argsToken[i], mParams[i].ParameterType);
                break;
            }
        }

        if (target == null)
            return $"{{\"ok\":false,\"error\":\"method_not_found\",\"method\":\"{Escape(methodName)}\"}}";

        try
        {
            var ret = target.Invoke(comp, methodArgs);
            string retJson = ret == null ? "null" : SerializeValue(ret);
            return $"{{\"ok\":true,\"result\":{{\"return\":{retJson}}}}}";
        }
        catch (Exception e)
        {
            return $"{{\"ok\":false,\"error\":\"invocation_failed\",\"message\":\"{Escape(e.InnerException?.Message ?? e.Message)}\"}}";
        }
    }

    private static string SendEvent(JObject a)
    {
        string path = (string)a["path"];
        string message = (string)a["message"];

        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(message))
            return "{\"ok\":false,\"error\":\"missing_required_args\"}";

        var go = FindByHierarchyPath(path);
        if (go == null) return "{\"ok\":false,\"error\":\"gameobject_not_found\"}";

        var arg = a["arg"];
        if (arg == null || arg.Type == JTokenType.Null)
            go.SendMessage(message, SendMessageOptions.DontRequireReceiver);
        else
            go.SendMessage(message, ConvertJToken(arg, typeof(object)), SendMessageOptions.DontRequireReceiver);

        return "{\"ok\":true,\"result\":{\"sent\":true}}";
    }

    private static string ControlPlayMode(JObject a)
    {
        var action = (string)a["action"];

        if (action == "get_state")
        {
            return "{\"ok\":true,\"result\":{"
                + $"\"is_playing\":{(EditorApplication.isPlaying ? "true" : "false")},"
                + $"\"is_paused\":{(EditorApplication.isPaused ? "true" : "false")},"
                + $"\"timescale\":{Time.timeScale}"
                + "}}";
        }
        if (action == "enter")  { EditorApplication.isPlaying = true;  return "{\"ok\":true,\"result\":{\"action\":\"enter_play_mode\"}}"; }
        if (action == "exit")   { EditorApplication.isPlaying = false; return "{\"ok\":true,\"result\":{\"action\":\"exit_play_mode\"}}"; }
        if (action == "pause")  { EditorApplication.isPaused = true;   return "{\"ok\":true,\"result\":{\"action\":\"paused\"}}"; }
        if (action == "resume") { EditorApplication.isPaused = false;  return "{\"ok\":true,\"result\":{\"action\":\"resumed\"}}"; }
        if (action == "step")   { EditorApplication.Step();            return "{\"ok\":true,\"result\":{\"action\":\"stepped_one_frame\"}}"; }
        if (action == "set_timescale")
        {
            float ts = Mathf.Clamp((float?)a["timescale"] ?? 1f, 0f, 100f);
            Time.timeScale = ts;
            return $"{{\"ok\":true,\"result\":{{\"timescale\":{ts}}}}}";
        }
        return "{\"ok\":false,\"error\":\"unknown_action\"}";
    }

    private static string QueryScene(JObject a)
    {
        string filterTag  = (string)a["filter_tag"]  ?? "";
        string filterComp = (string)a["filter_component"] ?? "";
        string filterName = (string)a["filter_name"] ?? "";
        int maxDepth = Mathf.Clamp((int?)a["max_depth"] ?? 5, 1, 10);
        bool includeInactive = (bool?)a["include_inactive"] ?? false;

        Type filterCompType = string.IsNullOrEmpty(filterComp) ? null : ResolveType(filterComp);

        var roots = SceneManager.GetActiveScene().GetRootGameObjects();
        var sb = new StringBuilder();
        sb.Append("{\"ok\":true,\"result\":{\"scene\":\"");
        sb.Append(Escape(SceneManager.GetActiveScene().name));
        sb.Append("\",\"is_playing\":");
        sb.Append(EditorApplication.isPlaying ? "true" : "false");
        sb.Append(",\"objects\":[");

        bool first = true;
        foreach (var root in roots)
            AppendGameObjectJson(sb, root, 0, maxDepth, filterTag, filterCompType, filterName, includeInactive, ref first);

        sb.Append("]}}");
        return sb.ToString();
    }

    private static void AppendGameObjectJson(
        StringBuilder sb, GameObject go, int depth, int maxDepth,
        string filterTag, Type filterCompType, string filterName,
        bool includeInactive, ref bool first)
    {
        if (!includeInactive && !go.activeInHierarchy) return;

        bool tagMatch  = string.IsNullOrEmpty(filterTag)  || go.CompareTag(filterTag);
        bool compMatch = filterCompType == null            || go.GetComponent(filterCompType) != null;
        bool nameMatch = string.IsNullOrEmpty(filterName) ||
                         go.name.IndexOf(filterName, StringComparison.OrdinalIgnoreCase) >= 0;

        if (tagMatch && compMatch && nameMatch)
        {
            if (!first) sb.Append(",");
            first = false;
            var t = go.transform;
            sb.Append("{");
            sb.Append($"\"path\":\"{Escape(GetHierarchyPath(go))}\",");
            sb.Append($"\"name\":\"{Escape(go.name)}\",");
            sb.Append($"\"active\":{(go.activeSelf ? "true" : "false")},");
            sb.Append($"\"tag\":\"{Escape(go.tag)}\",");
            sb.Append($"\"layer\":{go.layer},");
            sb.Append($"\"position\":[{t.position.x:F3},{t.position.y:F3},{t.position.z:F3}],");
            sb.Append($"\"rotation\":[{t.eulerAngles.x:F3},{t.eulerAngles.y:F3},{t.eulerAngles.z:F3}],");
            var comps = go.GetComponents<Component>();
            sb.Append("\"components\":[");
            for (int i = 0; i < comps.Length; i++)
            {
                sb.Append($"\"{Escape(comps[i].GetType().Name)}\"");
                if (i < comps.Length - 1) sb.Append(",");
            }
            sb.Append("],");
            sb.Append($"\"child_count\":{go.transform.childCount}");
            sb.Append("}");
        }

        if (depth < maxDepth - 1)
            for (int i = 0; i < go.transform.childCount; i++)
                AppendGameObjectJson(sb, go.transform.GetChild(i).gameObject,
                    depth + 1, maxDepth, filterTag, filterCompType, filterName, includeInactive, ref first);
    }

    private static string ManageAsset(JObject a)
    {
        var action = (string)a["action"];

        if (action == "find")
        {
            string filter = (string)a["filter"] ?? "t:Object";
            var guids = UnityEditor.AssetDatabase.FindAssets(filter);
            var sb = new StringBuilder();
            sb.Append("{\"ok\":true,\"result\":{\"assets\":[");
            int limit = Mathf.Min(guids.Length, 50);
            for (int i = 0; i < limit; i++)
            {
                string assetPath = UnityEditor.AssetDatabase.GUIDToAssetPath(guids[i]);
                sb.Append($"\"{Escape(assetPath)}\"");
                if (i < limit - 1) sb.Append(",");
            }
            sb.Append($"],\"total\":{guids.Length}}}}}");
            return sb.ToString();
        }

        if (action == "instantiate")
        {
            string assetPath = (string)a["asset_path"];
            if (string.IsNullOrEmpty(assetPath)) return "{\"ok\":false,\"error\":\"missing_asset_path\"}";

            var prefab = UnityEditor.AssetDatabase.LoadAssetAtPath<GameObject>(assetPath);
            if (prefab == null) return "{\"ok\":false,\"error\":\"asset_not_found\"}";

            var instance = (GameObject)UnityEditor.PrefabUtility.InstantiatePrefab(prefab);
            if (instance == null) return "{\"ok\":false,\"error\":\"instantiate_failed\"}";

            if (a["position"] is JArray p && p.Count >= 3)
                instance.transform.position = new Vector3((float)p[0], (float)p[1], (float)p[2]);

            string parentPath = (string)a["parent_path"];
            if (!string.IsNullOrEmpty(parentPath))
            {
                var parent = FindByHierarchyPath(parentPath);
                if (parent != null) instance.transform.SetParent(parent.transform, true);
            }

            Undo.RegisterCreatedObjectUndo(instance, "Instantiate Prefab");
            EditorSceneManager.MarkSceneDirty(SceneManager.GetActiveScene());
            return $"{{\"ok\":true,\"result\":{{\"path\":\"{Escape(GetHierarchyPath(instance))}\"}}}}";
        }

        return "{\"ok\":false,\"error\":\"unknown_action\"}";
    }

    // ======== Main Thread Execution Helper (Thread-safe) ========

    private static string RunOnMainThread(Func<string> fn)
    {
        // ManualResetEventSlim is safe across domain reloads and does not
        // block the thread in a spin loop — the OS scheduler handles waiting.
        var mre    = new ManualResetEventSlim(false);
        string result = null;
        Exception err = null;

        EditorApplication.delayCall += () =>
        {
            try   { result = fn(); }
            catch (Exception e) { err = e; }
            finally { mre.Set(); }
        };

        // Wait up to 10 s; if the editor is in the middle of a domain reload
        // the delayCall may never fire — we time out gracefully.
        bool signalled = mre.Wait(TimeSpan.FromSeconds(10));
        if (!signalled)
            return "{\"ok\":false,\"error\":\"timeout_main_thread\"}";

        if (err != null)
            return $"{{\"ok\":false,\"error\":\"exception\",\"message\":\"{Escape(err.Message)}\"}}";

        return result;
    }

    // ======== Serialization Helpers ========

    private static string SerializeValue(object val)
    {
        if (val == null) return "null";
        if (val is bool b) return b ? "true" : "false";
        if (val is string s) return $"\"{Escape(s)}\"";
        if (val is int || val is long || val is short || val is byte)
            return Convert.ToInt64(val).ToString();
        if (val is float f) return f.ToString("G", System.Globalization.CultureInfo.InvariantCulture);
        if (val is double d) return d.ToString("G", System.Globalization.CultureInfo.InvariantCulture);
        if (val is Vector2 v2) return $"[{v2.x},{v2.y}]";
        if (val is Vector3 v3) return $"[{v3.x},{v3.y},{v3.z}]";
        if (val is Vector4 v4) return $"[{v4.x},{v4.y},{v4.z},{v4.w}]";
        if (val is Color c) return $"[{c.r},{c.g},{c.b},{c.a}]";
        if (val is Quaternion q) return $"[{q.x},{q.y},{q.z},{q.w}]";
        if (val is Enum) return $"\"{Escape(val.ToString())}\"";
        try { return JsonConvert.SerializeObject(val); }
        catch { return $"\"{Escape(val.ToString())}\""; }
    }

    private static object ConvertJToken(JToken token, Type targetType)
    {
        if (targetType == typeof(string))  return (string)token;
        if (targetType == typeof(int)   || targetType == typeof(int?))    return (int)token;
        if (targetType == typeof(float) || targetType == typeof(float?))  return (float)token;
        if (targetType == typeof(double)|| targetType == typeof(double?)) return (double)token;
        if (targetType == typeof(bool)  || targetType == typeof(bool?))   return (bool)token;
        if (targetType == typeof(Vector3) && token is JArray arr3 && arr3.Count >= 3)
            return new Vector3((float)arr3[0], (float)arr3[1], (float)arr3[2]);
        if (targetType == typeof(Vector2) && token is JArray arr2 && arr2.Count >= 2)
            return new Vector2((float)arr2[0], (float)arr2[1]);
        if (targetType == typeof(Color) && token is JArray arrC && arrC.Count >= 3)
            return new Color((float)arrC[0], (float)arrC[1], (float)arrC[2], arrC.Count >= 4 ? (float)arrC[3] : 1f);
        if (targetType.IsEnum)
            return Enum.Parse(targetType, (string)token, ignoreCase: true);
        if (targetType == typeof(object))
        {
            if (token.Type == JTokenType.String)  return (string)token;
            if (token.Type == JTokenType.Integer) return (int)token;
            if (token.Type == JTokenType.Float)   return (float)token;
            if (token.Type == JTokenType.Boolean) return (bool)token;
            return token.ToString();
        }
        return token.ToObject(targetType);
    }

    // ======== Utilities ========

    private static string[] TailLines(string path, int maxLines)
    {
        var all = File.ReadAllLines(path);
        int start = Mathf.Max(0, all.Length - maxLines);
        int len   = all.Length - start;
        var slice = new string[len];
        Array.Copy(all, start, slice, 0, len);
        return slice;
    }

    private static Type ResolveType(string typeName)
    {
        var t = Type.GetType(typeName);
        if (t != null) return t;
        t = Type.GetType(typeName + ",UnityEngine");
        if (t != null) return t;
        t = Type.GetType(typeName + ",UnityEditor");
        if (t != null) return t;
        foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
        {
            try { t = asm.GetType(typeName); if (t != null) return t; } catch { }
        }
        return null;
    }

    private static string ReadBody(HttpListenerRequest req)
    {
        using (var reader = new StreamReader(req.InputStream, req.ContentEncoding))
            return reader.ReadToEnd();
    }

    private static void WriteJson(HttpListenerResponse res, int code, string json)
    {
        var bytes = Encoding.UTF8.GetBytes(json);
        res.StatusCode       = code;
        res.ContentType      = "application/json";
        res.ContentEncoding  = Encoding.UTF8;
        res.ContentLength64  = bytes.Length;
        try { using (var output = res.OutputStream) output.Write(bytes, 0, bytes.Length); }
        catch { }
    }

    private static string Escape(string s)
    {
        if (s == null) return "";
        return s.Replace("\\", "\\\\")
                .Replace("\"", "\\\"")
                .Replace("\n", "\\n")
                .Replace("\r", "\\r")
                .Replace("\t", "\\t");
    }

    private static string GetHierarchyPath(GameObject go)
    {
        var t = go.transform;
        string path = t.name;
        while (t.parent != null) { t = t.parent; path = t.name + "/" + path; }
        return path;
    }

    private static GameObject FindByHierarchyPath(string path)
    {
        var parts = path.Split('/');
        if (parts.Length == 0) return null;
        var roots = SceneManager.GetActiveScene().GetRootGameObjects();
        GameObject cur = null;
        foreach (var r in roots) { if (r.name == parts[0]) { cur = r; break; } }
        if (cur == null) return null;
        for (int i = 1; i < parts.Length; i++)
        {
            var child = cur.transform.Find(parts[i]);
            if (child == null) return null;
            cur = child.gameObject;
        }
        return cur;
    }
}

// ======== Settings Window ========

public class UnityMcpBridgeSettingsWindow : EditorWindow
{
    private int    _port;
    private string _token;
    private bool   _autoStart;

    public static void ShowWindow()
    {
        var win = GetWindow<UnityMcpBridgeSettingsWindow>("Unity MCP Bridge v2.1");
        win.minSize = new Vector2(560, 300);
        win.Show();
    }

    private void OnEnable()
    {
        _port      = EditorPrefs.GetInt   ("MultiMCP.Unity.Port",      23457);
        _token     = EditorPrefs.GetString("MultiMCP.Unity.Token",     "");
        _autoStart = EditorPrefs.GetBool  ("MultiMCP.Unity.AutoStart", true);  // default true
    }

    private void OnGUI()
    {
        // ── Title + live status ──────────────────────────────────────────
        GUILayout.Label("Unity MCP Bridge v2.1 Settings", EditorStyles.boldLabel);
        GUILayout.Space(4);

        bool running = UnityMcpBridge.IsRunning;
        var statusStyle = new GUIStyle(EditorStyles.boldLabel);
        statusStyle.normal.textColor = running ? new Color(0.2f, 0.8f, 0.2f) : new Color(0.9f, 0.3f, 0.3f);
        GUILayout.Label(running
            ? $"● Server RUNNING  on port {_port}  (http://127.0.0.1:{_port}/mcp)"
            : "● Server STOPPED  — click [Start Server] below",
            statusStyle);

        GUILayout.Space(8);

        // ── Settings fields ──────────────────────────────────────────────
        _port      = EditorGUILayout.IntField ("Port (default: 23457)", _port);
        _token     = EditorGUILayout.TextField("Auth Token (optional)", _token);
        _autoStart = EditorGUILayout.Toggle   ("Auto Start on Editor Load", _autoStart);

        GUILayout.Space(8);

        // ── Buttons ──────────────────────────────────────────────────────
        GUILayout.BeginHorizontal();

        if (!running)
        {
            var startStyle = new GUIStyle(GUI.skin.button);
            startStyle.normal.textColor  = Color.white;
            startStyle.fontStyle         = FontStyle.Bold;
            if (GUILayout.Button("▶  Start Server", startStyle, GUILayout.Height(28)))
            {
                // Apply settings first, then start
                EditorPrefs.SetInt   ("MultiMCP.Unity.Port",      _port);
                EditorPrefs.SetString("MultiMCP.Unity.Token",     _token);
                EditorPrefs.SetBool  ("MultiMCP.Unity.AutoStart", _autoStart);
                UnityMcpBridge.StartServer();
            }
        }
        else
        {
            if (GUILayout.Button("■  Stop Server", GUILayout.Height(28)))
                UnityMcpBridge.StopServer();
        }

        if (GUILayout.Button("Save Settings", GUILayout.Height(28)))
        {
            EditorPrefs.SetInt   ("MultiMCP.Unity.Port",      _port);
            EditorPrefs.SetString("MultiMCP.Unity.Token",     _token);
            EditorPrefs.SetBool  ("MultiMCP.Unity.AutoStart", _autoStart);
            Debug.Log("[UnityMcpBridge] Settings saved.");
        }

        if (GUILayout.Button("Status", GUILayout.Height(28)))
            UnityMcpBridge.PrintStatus();

        GUILayout.EndHorizontal();

        GUILayout.Space(10);

        // ── Help box ─────────────────────────────────────────────────────
        string helpText = running
            ? $"✓ Server is running.\n\n" +
              $"MCP Endpoint : POST http://127.0.0.1:{_port}/mcp\n" +
              $"Health Check : GET  http://127.0.0.1:{_port}/health\n\n" +
              $"Register in Multi-MCP GUI:\n" +
              $"  Transport: http\n" +
              $"  Endpoint : http://127.0.0.1:{_port}/mcp"
            : "⚠  Server is NOT running.\n\n" +
              "  1. (Optional) Change port above.\n" +
              "  2. Click  ▶ Start Server  above.\n" +
              "  3. Enable 'Auto Start' to start automatically on next Editor load.\n\n" +
              "If Start fails, check the Unity Console for the error message.";

        EditorGUILayout.HelpBox(helpText,
            running ? MessageType.Info : MessageType.Warning);

        // Repaint every frame so the status label updates immediately
        Repaint();
    }
}
