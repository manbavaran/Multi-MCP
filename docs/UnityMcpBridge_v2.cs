// Assets/Editor/UnityMcpBridge.cs  (v2 — AI Autonomous Loop Ready)
// Multi-MCP compatible Unity Editor bridge (HTTP + MCP JSON-RPC over HTTP)
//
// CHANGES FROM v1:
//  + unity.get_component_property  — read any serialized field/property on a component
//  + unity.set_component_property  — write any serialized field/property on a component
//  + unity.call_component_method   — invoke a public method on a component (with args)
//  + unity.control_playmode        — enter/exit/pause/step play mode; set Time.timeScale
//  + unity.query_scene             — structured snapshot of scene hierarchy (AI observation)
//  + unity.manage_asset            — find/load/instantiate assets from the AssetDatabase
//  + unity.send_event              — send a named event to a component via SendMessage
//
// Existing tools (unchanged):
//  - unity.manage_gameobject       (find/get/create/delete/set_active/set_transform)
//  - unity.manage_scene            (list_open/save_active/open)
//  - unity.manage_components       (list/add/remove)
//  - unity.execute_menu_item       (execute Unity menu item)
//  - unity.read_console            (tail Editor.log)
//
// MCP endpoint: POST /mcp  (JSON-RPC 2.0)
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
    private static int Port = 23457;          // Different from Multi-MCP hub port (8765)
    private static string AuthToken = "";     // Optional Bearer token
    private static bool AutoStart = false;

    // ======== Server State ========
    private static HttpListener _listener;
    private static Thread _thread;
    private static volatile bool _running;

    static UnityMcpBridge()
    {
        Port = EditorPrefs.GetInt("MultiMCP.Unity.Port", 23457);
        AuthToken = EditorPrefs.GetString("MultiMCP.Unity.Token", "");
        AutoStart = EditorPrefs.GetBool("MultiMCP.Unity.AutoStart", false);

        if (AutoStart)
            StartServer();
    }

    // ======== Menu Items ========

    [MenuItem("Multi-MCP/Unity Bridge/Start")]
    public static void StartServer()
    {
        if (_running) { Debug.Log("[UnityMcpBridge] Already running."); return; }

        var prefix = $"http://127.0.0.1:{Port}/";
        _listener = new HttpListener();
        _listener.Prefixes.Add(prefix);

        try { _listener.Start(); }
        catch (Exception e)
        {
            Debug.LogError($"[UnityMcpBridge] Failed to start: {e.Message}");
            return;
        }

        _running = true;
        _thread = new Thread(ServerLoop) { IsBackground = true, Name = "UnityMcpBridge" };
        _thread.Start();

        Debug.Log($"[UnityMcpBridge] v2 started at {prefix}");
        Debug.Log($"[UnityMcpBridge] MCP endpoint: POST {prefix}mcp");
    }

    [MenuItem("Multi-MCP/Unity Bridge/Stop")]
    public static void StopServer()
    {
        _running = false;
        try { _listener?.Stop(); } catch { }
        try { _listener?.Close(); } catch { }
        _listener = null;
        try { _thread?.Join(1000); } catch { }
        _thread = null;
        Debug.Log("[UnityMcpBridge] Stopped.");
    }

    [MenuItem("Multi-MCP/Unity Bridge/Settings")]
    public static void Settings() => UnityMcpBridgeSettingsWindow.ShowWindow();

    [MenuItem("Multi-MCP/Unity Bridge/Status")]
    public static void PrintStatus()
    {
        Debug.Log($"[UnityMcpBridge] Running={_running}  Port={Port}  Auth={(!string.IsNullOrEmpty(AuthToken) ? "enabled" : "disabled")}");
    }

    // ======== Server Loop ========

    private static void ServerLoop()
    {
        while (_running && _listener != null && _listener.IsListening)
        {
            HttpListenerContext ctx = null;
            try
            {
                ctx = _listener.GetContext();
                HandleRequest(ctx);
            }
            catch (Exception e)
            {
                if (_running)
                    Debug.LogWarning($"[UnityMcpBridge] Request error: {e.Message}");
            }
        }
    }

    private static void HandleRequest(HttpListenerContext ctx)
    {
        var req = ctx.Request;
        var res = ctx.Response;

        // CORS for local dev tools
        res.Headers.Add("Access-Control-Allow-Origin", "http://localhost:8765");
        res.Headers.Add("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
        res.Headers.Add("Access-Control-Allow-Headers", "Content-Type, Authorization");

        if (req.HttpMethod == "OPTIONS")
        {
            res.StatusCode = 204;
            res.Close();
            return;
        }

        Debug.Log($"[UnityMcpBridge] {req.HttpMethod} {req.Url.AbsolutePath}");

        // Auth check
        if (!string.IsNullOrEmpty(AuthToken))
        {
            var auth = req.Headers["Authorization"] ?? "";
            if (auth != $"Bearer {AuthToken}")
            {
                WriteJson(res, 401, "{\"error\":\"unauthorized\"}");
                return;
            }
        }

        // MCP JSON-RPC 2.0
        if (req.HttpMethod == "POST" && req.Url.AbsolutePath == "/mcp")
        {
            WriteJson(res, 200, HandleJsonRpc(ReadBody(req)));
            return;
        }

        // Health check (for Multi-MCP discovery)
        if (req.HttpMethod == "GET" && req.Url.AbsolutePath == "/health")
        {
            WriteJson(res, 200, $"{{\"status\":\"ok\",\"service\":\"unity-mcp-bridge\",\"version\":\"2\",\"port\":{Port}}}");
            return;
        }

        // Legacy endpoints
        if (req.HttpMethod == "GET" && req.Url.AbsolutePath == "/tools/list")
        {
            WriteJson(res, 200, ToolsListLegacy());
            return;
        }

        if (req.HttpMethod == "POST" && req.Url.AbsolutePath == "/tools/call")
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
            var p = (JObject)(req["params"] ?? new JObject());
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
      ""input_schema"": {
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
      ""input_schema"": {
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
      ""input_schema"": {
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
      ""description"": ""Read a field or property value from a component on a GameObject. Supports public fields, serialized fields, and public properties. Use this to observe game state (HP, score, flags, etc.)"",
      ""input_schema"": {
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
      ""description"": ""Write a value to a field or property on a component. Use to modify game state (HP, score, speed, flags, etc.)"",
      ""input_schema"": {
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
      ""description"": ""Invoke a public method on a component. Use to trigger game actions (Attack, OpenDoor, AddItem, StartQuest, etc.)"",
      ""input_schema"": {
        ""type"": ""object"",
        ""properties"": {
          ""path"":      { ""type"": ""string"" },
          ""component"": { ""type"": ""string"" },
          ""method"":    { ""type"": ""string"", ""description"": ""Method name (e.g. Attack, Heal, AddItem)"" },
          ""args"":      { ""type"": ""array"", ""description"": ""Positional arguments (string/number/boolean)"", ""items"": {} }
        },
        ""required"": [""path"",""component"",""method""]
      }
    },
    {
      ""name"": ""unity.send_event"",
      ""description"": ""Send a named message to a component via SendMessage (no return value). Useful for triggering event-driven behaviours."",
      ""input_schema"": {
        ""type"": ""object"",
        ""properties"": {
          ""path"":    { ""type"": ""string"" },
          ""message"": { ""type"": ""string"", ""description"": ""Method name to broadcast (e.g. OnPlayerDied)"" },
          ""arg"":     { ""description"": ""Optional single argument (string/number/boolean)"" }
        },
        ""required"": [""path"",""message""]
      }
    },
    {
      ""name"": ""unity.control_playmode"",
      ""description"": ""Control Unity Editor play mode and time. Use to start/stop/pause the game, advance frames, or change time scale for simulation."",
      ""input_schema"": {
        ""type"": ""object"",
        ""properties"": {
          ""action"":     { ""type"": ""string"", ""enum"": [""enter"",""exit"",""pause"",""resume"",""step"",""get_state"",""set_timescale""] },
          ""timescale"":  { ""type"": ""number"", ""minimum"": 0, ""maximum"": 100, ""description"": ""Time.timeScale value (1=normal, 2=double speed, 0=freeze)"" }
        },
        ""required"": [""action""]
      }
    },
    {
      ""name"": ""unity.query_scene"",
      ""description"": ""Return a structured snapshot of the active scene hierarchy. Use as an observation step before planning actions. Can filter by tag, layer, or component type."",
      ""input_schema"": {
        ""type"": ""object"",
        ""properties"": {
          ""filter_tag"":       { ""type"": ""string"", ""description"": ""Only include GameObjects with this tag (e.g. Player, Enemy)"" },
          ""filter_component"": { ""type"": ""string"", ""description"": ""Only include GameObjects that have this component type"" },
          ""filter_name"":      { ""type"": ""string"", ""description"": ""Substring match on GameObject name"" },
          ""max_depth"":        { ""type"": ""integer"", ""minimum"": 1, ""maximum"": 10, ""description"": ""Max hierarchy depth to traverse (default 5)"" },
          ""include_inactive"": { ""type"": ""boolean"", ""description"": ""Include inactive GameObjects (default false)"" }
        }
      }
    },
    {
      ""name"": ""unity.manage_asset"",
      ""description"": ""Find assets in the AssetDatabase and instantiate prefabs into the scene."",
      ""input_schema"": {
        ""type"": ""object"",
        ""properties"": {
          ""action"":      { ""type"": ""string"", ""enum"": [""find"",""instantiate""] },
          ""filter"":      { ""type"": ""string"", ""description"": ""Asset search filter (e.g. 't:Prefab Enemy')"" },
          ""asset_path"":  { ""type"": ""string"", ""description"": ""Asset path for instantiate (e.g. Assets/Prefabs/Enemy.prefab)"" },
          ""position"":    { ""type"": ""array"", ""items"": {""type"":""number""}, ""minItems"":3, ""maxItems"":3 },
          ""parent_path"": { ""type"": ""string"" }
        },
        ""required"": [""action""]
      }
    },
    {
      ""name"": ""unity.execute_menu_item"",
      ""description"": ""Execute a Unity Editor menu item by path (e.g. File/Save Project)"",
      ""input_schema"": {
        ""type"": ""object"",
        ""properties"": { ""menu"": { ""type"": ""string"" } },
        ""required"": [""menu""]
      }
    },
    {
      ""name"": ""unity.read_console"",
      ""description"": ""Tail the Unity Editor log. Use after actions to check for errors or confirm success."",
      ""input_schema"": {
        ""type"": ""object"",
        ""properties"": {
          ""max_lines"": { ""type"": ""integer"", ""minimum"": 1, ""maximum"": 2000 },
          ""filter"":    { ""type"": ""string"", ""description"": ""Substring filter on log lines"" }
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

    // ======== Tool Implementations (Original) ========

    private static string ManageGameObject(JObject a)
    {
        var action = (string)a["action"];

        if (action == "find")
        {
            string query = (string)a["query"] ?? "";
            var roots = SceneManager.GetActiveScene().GetRootGameObjects();
            var sb = new StringBuilder();
            sb.Append("{\"ok\":true,\"result\":{\"matches\":[");
            bool first = true;
            foreach (var r in roots)
            {
                foreach (var t in r.GetComponentsInChildren<Transform>(true))
                {
                    if (string.IsNullOrEmpty(query) || t.name.IndexOf(query, StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        if (!first) sb.Append(",");
                        sb.Append($"\"{Escape(GetHierarchyPath(t.gameObject))}\"");
                        first = false;
                    }
                }
            }
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
            return "{\"ok\":true,\"result\":{"
                + $"\"path\":\"{Escape(GetHierarchyPath(go))}\","
                + $"\"active\":{(go.activeSelf ? "true" : "false")},"
                + $"\"tag\":\"{Escape(go.tag)}\","
                + $"\"layer\":{go.layer},"
                + $"\"position\":[{t.position.x},{t.position.y},{t.position.z}],"
                + $"\"rotation\":[{t.eulerAngles.x},{t.eulerAngles.y},{t.eulerAngles.z}],"
                + $"\"scale\":[{t.localScale.x},{t.localScale.y},{t.localScale.z}]"
                + "}}";
        }

        if (action == "create")
        {
            string name = (string)a["name"] ?? "NewGameObject";
            string parentPath = (string)a["parent_path"];
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
            var lines = TailLines(logPath, maxLines * 3); // read more, then filter
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

    // ======== NEW Tool Implementations ========

    /// <summary>
    /// Read a field or property from a component.
    /// Supports: public fields, [SerializeField] fields, public properties.
    /// </summary>
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

        // Try field first (public + non-public serialized)
        var field = compType.GetField(propName,
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
        if (field != null)
        {
            var val = field.GetValue(comp);
            return $"{{\"ok\":true,\"result\":{{\"value\":{SerializeValue(val)},\"type\":\"{Escape(field.FieldType.Name)}\"}}}}";
        }

        // Try property
        var prop = compType.GetProperty(propName,
            BindingFlags.Public | BindingFlags.Instance);
        if (prop != null && prop.CanRead)
        {
            var val = prop.GetValue(comp);
            return $"{{\"ok\":true,\"result\":{{\"value\":{SerializeValue(val)},\"type\":\"{Escape(prop.PropertyType.Name)}\"}}}}";
        }

        return $"{{\"ok\":false,\"error\":\"member_not_found\",\"member\":\"{Escape(propName)}\"}}";
    }

    /// <summary>
    /// Write a value to a field or property on a component.
    /// </summary>
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

        // Try field
        var field = compType.GetField(propName,
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
        if (field != null)
        {
            try
            {
                var converted = ConvertJToken(value, field.FieldType);
                field.SetValue(comp, converted);
                EditorUtility.SetDirty(comp);
                return "{\"ok\":true,\"result\":{\"updated\":true}}";
            }
            catch (Exception e)
            {
                return $"{{\"ok\":false,\"error\":\"conversion_failed\",\"message\":\"{Escape(e.Message)}\"}}";
            }
        }

        // Try property
        var prop = compType.GetProperty(propName,
            BindingFlags.Public | BindingFlags.Instance);
        if (prop != null && prop.CanWrite)
        {
            try
            {
                var converted = ConvertJToken(value, prop.PropertyType);
                prop.SetValue(comp, converted);
                EditorUtility.SetDirty(comp);
                return "{\"ok\":true,\"result\":{\"updated\":true}}";
            }
            catch (Exception e)
            {
                return $"{{\"ok\":false,\"error\":\"conversion_failed\",\"message\":\"{Escape(e.Message)}\"}}";
            }
        }

        return $"{{\"ok\":false,\"error\":\"member_not_found_or_readonly\",\"member\":\"{Escape(propName)}\"}}";
    }

    /// <summary>
    /// Invoke a public method on a component.
    /// </summary>
    private static string CallComponentMethod(JObject a)
    {
        string path = (string)a["path"];
        string compName = (string)a["component"];
        string methodName = (string)a["method"];
        var argsToken = a["args"] as JArray;

        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(compName) || string.IsNullOrEmpty(methodName))
            return "{\"ok\":false,\"error\":\"missing_required_args\"}";

        var go = FindByHierarchyPath(path);
        if (go == null) return "{\"ok\":false,\"error\":\"gameobject_not_found\"}";

        var compType = ResolveType(compName);
        if (compType == null) return "{\"ok\":false,\"error\":\"component_type_not_found\"}";

        var comp = go.GetComponent(compType);
        if (comp == null) return "{\"ok\":false,\"error\":\"component_not_on_gameobject\"}";

        // Find method (public, instance) — try overloads
        var methods = compType.GetMethods(BindingFlags.Public | BindingFlags.Instance);
        MethodInfo target = null;
        foreach (var m in methods)
        {
            if (m.Name != methodName) continue;
            var mParams = m.GetParameters();
            int argCount = argsToken?.Count ?? 0;
            if (mParams.Length == argCount) { target = m; break; }
            // Accept zero-arg call even if method has optional params
            if (argCount == 0 && mParams.Length > 0 &&
                Array.TrueForAll(mParams, p => p.IsOptional)) { target = m; break; }
        }

        if (target == null)
            return $"{{\"ok\":false,\"error\":\"method_not_found\",\"method\":\"{Escape(methodName)}\"}}";

        try
        {
            var mParams = target.GetParameters();
            object[] callArgs = new object[mParams.Length];
            for (int i = 0; i < mParams.Length; i++)
            {
                if (argsToken != null && i < argsToken.Count)
                    callArgs[i] = ConvertJToken(argsToken[i], mParams[i].ParameterType);
                else if (mParams[i].IsOptional)
                    callArgs[i] = mParams[i].DefaultValue;
            }

            var returnVal = target.Invoke(comp, callArgs);
            string returnJson = returnVal != null ? SerializeValue(returnVal) : "null";
            return $"{{\"ok\":true,\"result\":{{\"return\":{returnJson}}}}}";
        }
        catch (Exception e)
        {
            return $"{{\"ok\":false,\"error\":\"invocation_failed\",\"message\":\"{Escape(e.InnerException?.Message ?? e.Message)}\"}}";
        }
    }

    /// <summary>
    /// Send a Unity message to a component via SendMessage.
    /// </summary>
    private static string SendEvent(JObject a)
    {
        string path = (string)a["path"];
        string message = (string)a["message"];
        var arg = a["arg"];

        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(message))
            return "{\"ok\":false,\"error\":\"missing_required_args\"}";

        var go = FindByHierarchyPath(path);
        if (go == null) return "{\"ok\":false,\"error\":\"gameobject_not_found\"}";

        try
        {
            if (arg != null)
                go.SendMessage(message, ConvertJToken(arg, typeof(object)), SendMessageOptions.DontRequireReceiver);
            else
                go.SendMessage(message, SendMessageOptions.DontRequireReceiver);

            return "{\"ok\":true,\"result\":{\"sent\":true}}";
        }
        catch (Exception e)
        {
            return $"{{\"ok\":false,\"error\":\"send_failed\",\"message\":\"{Escape(e.Message)}\"}}";
        }
    }

    /// <summary>
    /// Control Unity Editor play mode and time scale.
    /// </summary>
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

        if (action == "enter")
        {
            EditorApplication.isPlaying = true;
            return "{\"ok\":true,\"result\":{\"action\":\"enter_play_mode\"}}";
        }

        if (action == "exit")
        {
            EditorApplication.isPlaying = false;
            return "{\"ok\":true,\"result\":{\"action\":\"exit_play_mode\"}}";
        }

        if (action == "pause")
        {
            EditorApplication.isPaused = true;
            return "{\"ok\":true,\"result\":{\"action\":\"paused\"}}";
        }

        if (action == "resume")
        {
            EditorApplication.isPaused = false;
            return "{\"ok\":true,\"result\":{\"action\":\"resumed\"}}";
        }

        if (action == "step")
        {
            EditorApplication.Step();
            return "{\"ok\":true,\"result\":{\"action\":\"stepped_one_frame\"}}";
        }

        if (action == "set_timescale")
        {
            float ts = (float?)a["timescale"] ?? 1f;
            ts = Mathf.Clamp(ts, 0f, 100f);
            Time.timeScale = ts;
            return $"{{\"ok\":true,\"result\":{{\"timescale\":{ts}}}}}";
        }

        return "{\"ok\":false,\"error\":\"unknown_action\"}";
    }

    /// <summary>
    /// Return a structured snapshot of the scene hierarchy for AI observation.
    /// </summary>
    private static string QueryScene(JObject a)
    {
        string filterTag = (string)a["filter_tag"] ?? "";
        string filterComp = (string)a["filter_component"] ?? "";
        string filterName = (string)a["filter_name"] ?? "";
        int maxDepth = (int?)a["max_depth"] ?? 5;
        bool includeInactive = (bool?)a["include_inactive"] ?? false;

        maxDepth = Mathf.Clamp(maxDepth, 1, 10);

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
        {
            AppendGameObjectJson(sb, root, 0, maxDepth, filterTag, filterCompType, filterName, includeInactive, ref first);
        }

        sb.Append("]}}");
        return sb.ToString();
    }

    private static void AppendGameObjectJson(
        StringBuilder sb, GameObject go, int depth, int maxDepth,
        string filterTag, Type filterCompType, string filterName,
        bool includeInactive, ref bool first)
    {
        if (!includeInactive && !go.activeInHierarchy) return;

        bool tagMatch = string.IsNullOrEmpty(filterTag) || go.CompareTag(filterTag);
        bool compMatch = filterCompType == null || go.GetComponent(filterCompType) != null;
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

            // Component list (type names only)
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
        {
            for (int i = 0; i < go.transform.childCount; i++)
            {
                AppendGameObjectJson(sb, go.transform.GetChild(i).gameObject,
                    depth + 1, maxDepth, filterTag, filterCompType, filterName, includeInactive, ref first);
            }
        }
    }

    /// <summary>
    /// Find assets in the AssetDatabase or instantiate prefabs.
    /// </summary>
    private static string ManageAsset(JObject a)
    {
        var action = (string)a["action"];

        if (action == "find")
        {
            string filter = (string)a["filter"] ?? "t:Object";
            var guids = UnityEditor.AssetDatabase.FindAssets(filter);
            var sb = new StringBuilder();
            sb.Append("{\"ok\":true,\"result\":{\"assets\":[");
            int limit = Mathf.Min(guids.Length, 50); // cap results
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

            // Set position if provided
            if (a["position"] is JArray p && p.Count >= 3)
                instance.transform.position = new Vector3((float)p[0], (float)p[1], (float)p[2]);

            // Set parent if provided
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

    // ======== Main Thread Execution Helper ========

    private static string RunOnMainThread(Func<string> fn)
    {
        string result = null;
        Exception err = null;
        bool done = false;

        EditorApplication.delayCall += () =>
        {
            try { result = fn(); }
            catch (Exception e) { err = e; }
            finally { done = true; }
        };

        var start = DateTime.UtcNow;
        while (!done)
        {
            Thread.Sleep(10);
            if ((DateTime.UtcNow - start).TotalSeconds > 10)
                return "{\"ok\":false,\"error\":\"timeout_main_thread\"}";
        }

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
        // Fallback: JSON serialize
        try { return JsonConvert.SerializeObject(val); }
        catch { return $"\"{Escape(val.ToString())}\""; }
    }

    private static object ConvertJToken(JToken token, Type targetType)
    {
        if (targetType == typeof(string)) return (string)token;
        if (targetType == typeof(int) || targetType == typeof(int?)) return (int)token;
        if (targetType == typeof(float) || targetType == typeof(float?)) return (float)token;
        if (targetType == typeof(double) || targetType == typeof(double?)) return (double)token;
        if (targetType == typeof(bool) || targetType == typeof(bool?)) return (bool)token;
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
            if (token.Type == JTokenType.String) return (string)token;
            if (token.Type == JTokenType.Integer) return (int)token;
            if (token.Type == JTokenType.Float) return (float)token;
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
        int len = all.Length - start;
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
        res.StatusCode = code;
        res.ContentType = "application/json";
        res.ContentEncoding = Encoding.UTF8;
        res.ContentLength64 = bytes.Length;
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
    private int _port;
    private string _token;
    private bool _autoStart;

    public static void ShowWindow()
    {
        var win = GetWindow<UnityMcpBridgeSettingsWindow>("Unity MCP Bridge v2");
        win.minSize = new Vector2(560, 280);
        win.Show();
    }

    private void OnEnable()
    {
        _port = EditorPrefs.GetInt("MultiMCP.Unity.Port", 23457);
        _token = EditorPrefs.GetString("MultiMCP.Unity.Token", "");
        _autoStart = EditorPrefs.GetBool("MultiMCP.Unity.AutoStart", false);
    }

    private void OnGUI()
    {
        GUILayout.Label("Unity MCP Bridge v2 Settings", EditorStyles.boldLabel);
        GUILayout.Space(4);

        _port = EditorGUILayout.IntField("Port (default: 23457)", _port);
        _token = EditorGUILayout.TextField("Auth Token (optional)", _token);
        _autoStart = EditorGUILayout.Toggle("Auto Start on Editor Load", _autoStart);

        GUILayout.Space(8);

        if (GUILayout.Button("Save Settings"))
        {
            EditorPrefs.SetInt("MultiMCP.Unity.Port", _port);
            EditorPrefs.SetString("MultiMCP.Unity.Token", _token);
            EditorPrefs.SetBool("MultiMCP.Unity.AutoStart", _autoStart);
            Debug.Log("[UnityMcpBridge] Settings saved. Restart server to apply changes.");
        }

        GUILayout.Space(8);
        GUILayout.BeginHorizontal();
        if (GUILayout.Button("Start Server")) UnityMcpBridge.StartServer();
        if (GUILayout.Button("Stop Server")) UnityMcpBridge.StopServer();
        if (GUILayout.Button("Status")) UnityMcpBridge.PrintStatus();
        GUILayout.EndHorizontal();

        GUILayout.Space(10);
        EditorGUILayout.HelpBox(
            $"MCP Endpoint:  POST http://127.0.0.1:{_port}/mcp\n" +
            $"Health Check:  GET  http://127.0.0.1:{_port}/health\n" +
            $"Legacy List:   GET  http://127.0.0.1:{_port}/tools/list\n\n" +
            "Register in Multi-MCP GUI:\n" +
            $"  Transport: http   Endpoint: http://127.0.0.1:{_port}/mcp",
            MessageType.Info);
    }
}
