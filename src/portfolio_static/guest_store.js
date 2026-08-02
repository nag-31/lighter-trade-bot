(function () {
  "use strict";

  var DB_NAME = "portfolio-guest";
  var DB_VERSION = 1;
  var MAX_SNAPSHOTS_PER_WALLET = 1000;
  var dbPromise = null;

  function requestValue(request) {
    return new Promise(function (resolve, reject) {
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error || new Error("IndexedDB request failed")); };
    });
  }

  function transactionDone(transaction) {
    return new Promise(function (resolve, reject) {
      transaction.oncomplete = function () { resolve(); };
      transaction.onerror = function () { reject(transaction.error || new Error("IndexedDB transaction failed")); };
      transaction.onabort = function () { reject(transaction.error || new Error("IndexedDB transaction aborted")); };
    });
  }

  function open() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(function (resolve, reject) {
      if (!window.indexedDB) {
        reject(new Error("This browser does not support IndexedDB"));
        return;
      }
      var request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = function () {
        var db = request.result;
        if (!db.objectStoreNames.contains("wallets")) {
          var wallets = db.createObjectStore("wallets", { keyPath: "id" });
          wallets.createIndex("address", "address", { unique: true });
        }
        if (!db.objectStoreNames.contains("snapshots")) {
          var snapshots = db.createObjectStore("snapshots", { keyPath: "id", autoIncrement: true });
          snapshots.createIndex("address_id", "address_id", { unique: false });
          snapshots.createIndex("ts", "ts", { unique: false });
        }
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error || new Error("Could not open local portfolio storage")); };
    });
    return dbPromise;
  }

  function getAll(storeName) {
    return open().then(function (db) {
      return requestValue(db.transaction(storeName, "readonly").objectStore(storeName).getAll());
    });
  }

  function listWallets() {
    return getAll("wallets").then(function (rows) {
      return rows.filter(function (row) { return row.enabled !== false; })
        .sort(function (a, b) { return Number(a.id) - Number(b.id); });
    });
  }

  function upsertWallets(items) {
    return listWallets().then(function (existing) {
      var byAddress = {};
      var maxId = 0;
      existing.forEach(function (wallet) {
        byAddress[wallet.address] = wallet;
        maxId = Math.max(maxId, Number(wallet.id) || 0);
      });
      var now = new Date().toISOString();
      var rows = items.map(function (item) {
        var current = byAddress[item.address];
        if (current) {
          current.label = item.label != null ? item.label : current.label;
          current.enabled = true;
          current.updated_at = now;
          return current;
        }
        maxId += 1;
        var row = {
          id: maxId,
          address: item.address,
          label: item.label || null,
          enabled: true,
          excluded: false,
          created_at: now,
          updated_at: now
        };
        byAddress[item.address] = row;
        return row;
      });
      return open().then(function (db) {
        var tx = db.transaction("wallets", "readwrite");
        rows.forEach(function (row) { tx.objectStore("wallets").put(row); });
        return transactionDone(tx).then(function () { return rows; });
      });
    });
  }

  function updateWallet(id, fields) {
    return open().then(function (db) {
      return requestValue(db.transaction("wallets", "readonly").objectStore("wallets").get(Number(id))).then(function (wallet) {
        if (!wallet) return null;
        Object.keys(fields || {}).forEach(function (key) {
          if (key === "label" || key === "excluded") wallet[key] = fields[key];
        });
        wallet.updated_at = new Date().toISOString();
        var writeTx = db.transaction("wallets", "readwrite");
        writeTx.objectStore("wallets").put(wallet);
        return transactionDone(writeTx).then(function () { return wallet; });
      });
    });
  }

  function removeWallet(id) {
    id = Number(id);
    return open().then(function (db) {
      var tx = db.transaction(["wallets", "snapshots"], "readwrite");
      tx.objectStore("wallets").delete(id);
      var cursorRequest = tx.objectStore("snapshots").index("address_id").openCursor(IDBKeyRange.only(id));
      cursorRequest.onsuccess = function () {
        var cursor = cursorRequest.result;
        if (!cursor) return;
        cursor.delete();
        cursor.continue();
      };
      return transactionDone(tx).then(function () { return true; });
    });
  }

  function pruneSnapshots(addressId) {
    return open().then(function (db) {
      return requestValue(
        db.transaction("snapshots", "readonly").objectStore("snapshots")
          .index("address_id").getAll(IDBKeyRange.only(Number(addressId)))
      ).then(function (rows) {
        rows.sort(function (a, b) { return Number(a.id) - Number(b.id); });
        var stale = rows.slice(0, Math.max(0, rows.length - MAX_SNAPSHOTS_PER_WALLET));
        if (!stale.length) return;
        var writeTx = db.transaction("snapshots", "readwrite");
        stale.forEach(function (row) { writeTx.objectStore("snapshots").delete(row.id); });
        return transactionDone(writeTx);
      });
    });
  }

  function saveSnapshot(addressId, payload) {
    var row = {
      address_id: Number(addressId),
      ts: String(payload.timestamp || new Date().toISOString()),
      status: String(payload.status || "ok"),
      total_usd: Number((payload.totals || {}).total_usd || 0),
      payload: payload,
      error: (payload.errors || []).join("\n") || null
    };
    return open().then(function (db) {
      var tx = db.transaction("snapshots", "readwrite");
      var request = tx.objectStore("snapshots").add(row);
      return requestValue(request).then(function (id) {
        row.id = id;
        return transactionDone(tx).then(function () {
          return pruneSnapshots(addressId).then(function () { return row; });
        });
      });
    });
  }

  function latestSnapshots() {
    return getAll("snapshots").then(function (rows) {
      var latest = {};
      var successful = {};
      rows.forEach(function (row) {
        var id = Number(row.address_id);
        if (!latest[id] || Number(row.id) > Number(latest[id].id)) latest[id] = row;
        if (row.status !== "error" && (!successful[id] || Number(row.id) > Number(successful[id].id))) successful[id] = row;
      });
      return { latest: latest, successful: successful };
    });
  }

  function addressHistory(addressId, limit) {
    return open().then(function (db) {
      return requestValue(
        db.transaction("snapshots", "readonly").objectStore("snapshots")
          .index("address_id").getAll(IDBKeyRange.only(Number(addressId)))
      );
    }).then(function (rows) {
      rows = rows.filter(function (row) { return row.status !== "error"; })
        .sort(function (a, b) { return Number(a.id) - Number(b.id); });
      if (limit >= 0) rows = rows.slice(-limit);
      return rows.map(function (row) {
        return { id: row.id, ts: row.ts, status: row.status, total_usd: row.total_usd };
      });
    });
  }

  function aggregateHistory(addressIds, limit) {
    var selected = {};
    (addressIds || []).forEach(function (id) { selected[Number(id)] = true; });
    if (!Object.keys(selected).length) return Promise.resolve([]);
    return Promise.all([listWallets(), getAll("snapshots")]).then(function (parts) {
      var enabled = {};
      parts[0].forEach(function (wallet) {
        if (!wallet.excluded && selected[Number(wallet.id)]) enabled[Number(wallet.id)] = true;
      });
      var rows = parts[1].filter(function (row) {
        return enabled[Number(row.address_id)] && row.status !== "error";
      }).sort(function (a, b) {
        var byTime = String(a.ts).localeCompare(String(b.ts));
        return byTime || Number(a.id) - Number(b.id);
      });
      var lastKnown = {};
      var total = 0;
      var points = rows.map(function (row) {
        var id = Number(row.address_id);
        var value = Number(row.total_usd || 0);
        // Updating a running total avoids rebuilding it for every snapshot.
        total += value - (lastKnown[id] || 0);
        lastKnown[id] = value;
        return { ts: row.ts, total_usd: total };
      });
      return limit >= 0 ? points.slice(-limit) : points;
    });
  }

  function exportData() {
    return Promise.all([getAll("wallets"), getAll("snapshots")]).then(function (parts) {
      return { version: 1, exported_at: new Date().toISOString(), wallets: parts[0], snapshots: parts[1] };
    });
  }

  function replaceData(data) {
    if (!data || data.version !== 1 || !Array.isArray(data.wallets) || !Array.isArray(data.snapshots)) {
      return Promise.reject(new Error("Unsupported backup format"));
    }
    return open().then(function (db) {
      var tx = db.transaction(["wallets", "snapshots"], "readwrite");
      var walletStore = tx.objectStore("wallets");
      var snapshotStore = tx.objectStore("snapshots");
      walletStore.clear();
      snapshotStore.clear();
      data.wallets.forEach(function (wallet) { walletStore.put(wallet); });
      data.snapshots.forEach(function (snapshot) {
        var row = Object.assign({}, snapshot);
        delete row.id;
        snapshotStore.add(row);
      });
      return transactionDone(tx);
    });
  }

  window.PortfolioGuestStore = {
    open: open,
    listWallets: listWallets,
    upsertWallets: upsertWallets,
    updateWallet: updateWallet,
    removeWallet: removeWallet,
    saveSnapshot: saveSnapshot,
    latestSnapshots: latestSnapshots,
    addressHistory: addressHistory,
    aggregateHistory: aggregateHistory,
    exportData: exportData,
    replaceData: replaceData
  };
})();
