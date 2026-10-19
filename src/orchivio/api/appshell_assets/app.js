/* The runtime shell of a generated application (docs/PWA.md): signs the end user in and renders the pages the server says they may open (list, form,
 * detail), from the page API. Everything that is shown or allowed is decided by the server; this file only draws it. It builds the page with DOM calls and
 * textContent, never innerHTML, so data from the application's database cannot inject markup. No data is stored on the device. */
(function () {
  'use strict'
  var API = new URL('../', document.baseURI).href // the application's API root: <api>/apps/<id>/
  var csrf = null
  var state = { user: null, pages: [] }
  var $ = function (id) { return document.getElementById(id) }
  var main = $('main')

  function el(tag, props, children) {
    var node = document.createElement(tag)
    Object.keys(props || {}).forEach(function (k) {
      if (k === 'text') node.textContent = props[k]
      else if (k === 'class') node.className = props[k]
      else if (k.slice(0, 2) === 'on') node.addEventListener(k.slice(2), props[k])
      else if (props[k] !== null && props[k] !== undefined && props[k] !== false) node.setAttribute(k, props[k] === true ? '' : props[k])
    })
    ;(children || []).forEach(function (c) { if (c) node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c) })
    return node
  }

  function ApiProblem(status, code, message, details) { this.status = status; this.code = code; this.message = message; this.details = details }

  function call(method, path, body) {
    var headers = { Accept: 'application/json' }
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    if (method !== 'GET' && csrf) headers['X-CSRF-Token'] = csrf
    return fetch(API + path, { method: method, headers: headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body) }).then(function (res) {
      if (res.status === 204) return null
      return res.text().then(function (text) {
        var data = null
        try { data = text ? JSON.parse(text) : null } catch (e) { /* not JSON */ }
        if (!res.ok) {
          var err = data && data.error ? data.error : {}
          throw new ApiProblem(res.status, err.code || 'http_error', err.message || 'Request failed (' + res.status + ')', err.details)
        }
        return data
      })
    })
  }

  function show(text, cls) { return el('p', { class: cls || 'hint', role: cls === 'error' ? 'alert' : null, text: text }) }
  function problem(e) {
    if (e instanceof ApiProblem) {
      var parts = Array.isArray(e.details) ? e.details.map(function (d) { return d && d.field ? d.field + ': ' + d.message : '' }).filter(Boolean) : []
      return e.message + (parts.length ? ' (' + parts.join('; ') + ')' : '')
    }
    return 'You are offline or the server cannot be reached.'
  }
  function render(nodes) { main.replaceChildren.apply(main, nodes) }

  // --- sign in ---------------------------------------------------------------------------------------------------------------------------------

  function loginView(message) {
    var user = el('input', { id: 'username', autocomplete: 'username', required: true })
    var pass = el('input', { id: 'password', type: 'password', autocomplete: 'current-password', required: true })
    var out = show(message || '', message ? 'error' : 'hint')
    var form = el('form', { class: 'card', onsubmit: function (ev) {
      ev.preventDefault()
      call('POST', 'auth/login', { username: user.value, password: pass.value }).then(function (r) {
        csrf = r.csrf_token
        return start(r.user)
      }).catch(function (e) { out.textContent = problem(e); out.className = 'error'; out.setAttribute('role', 'alert') })
    } }, [
      el('h2', { text: 'Sign in' }),
      el('label', {}, [el('span', { text: 'Username' }), user]),
      el('label', {}, [el('span', { text: 'Password' }), pass]),
      el('button', { class: 'primary', type: 'submit', text: 'Sign in' }),
      out,
    ])
    render([form])
  }

  function start(user) {
    state.user = user
    $('who').textContent = user.display_name || user.username || ''
    $('signout').hidden = user.anonymous === true
    if (user.must_change_password) return changePasswordView()
    return call('GET', 'pages').then(function (r) {
      state.pages = r.items
      route()
    })
  }

  function changePasswordView() {
    var cur = el('input', { type: 'password', autocomplete: 'current-password', required: true })
    var next = el('input', { type: 'password', autocomplete: 'new-password', required: true })
    var out = show('')
    render([el('form', { class: 'card', onsubmit: function (ev) {
      ev.preventDefault()
      call('POST', 'auth/change-password', { current_password: cur.value, new_password: next.value }).then(function () { return call('GET', 'auth/me') })
        .then(function (u) { return start(u) }).catch(function (e) { out.textContent = problem(e); out.className = 'error' })
    } }, [
      el('h2', { text: 'Choose a new password' }),
      el('label', {}, [el('span', { text: 'Current password' }), cur]),
      el('label', {}, [el('span', { text: 'New password' }), next]),
      el('button', { class: 'primary', type: 'submit', text: 'Change password' }), out,
    ])])
  }

  // --- navigation ------------------------------------------------------------------------------------------------------------------------------

  function menu(current) {
    var items = state.pages.filter(function (p) { return p.type === 'list' })
    return el('nav', { 'aria-label': 'Pages' }, [el('ul', {}, items.map(function (p) {
      return el('li', {}, [el('button', { type: 'button', 'aria-current': current === p.key ? 'page' : null, text: label(p), onclick: function () { location.hash = '#/p/' + p.key } })])
    }))])
  }
  function label(p) { return p.key.replace(/-/g, ' ').replace(/^./, function (c) { return c.toUpperCase() }) }
  function find(key) { return state.pages.filter(function (p) { return p.key === key })[0] }
  function formFor(entity, mode) { return state.pages.filter(function (p) { return p.type === 'form' && p.entity === entity && p.mode === mode })[0] }
  function detailFor(entity) { return state.pages.filter(function (p) { return p.type === 'detail' && p.entity === entity })[0] }

  function route() {
    if (!state.user) return loginView()
    var parts = location.hash.replace(/^#\/?/, '').split('/') // p/<key>[/<rowId>]
    var key = parts[0] === 'p' ? parts[1] : (state.pages.filter(function (p) { return p.type === 'list' })[0] || state.pages[0] || {}).key
    var page = key && find(key)
    if (!page) return render([menu(null), show(state.pages.length ? 'That page does not exist or you may not open it.' : 'There is nothing here that you may open.')])
    if (page.type === 'list') return listView(page, 0)
    if (page.type === 'form') return formView(page, parts[2])
    return detailView(page, parts[2])
  }

  // --- list ------------------------------------------------------------------------------------------------------------------------------------

  var PAGE_SIZE = 20
  function cell(v) { return v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v) }
  function columnsOf(page, rows) {
    if (page.fields && page.fields.length) return page.fields.map(function (f) { return { name: f.name, label: f.label } })
    return Object.keys(rows[0] || {}).filter(function (k) { return k !== 'id' && k !== '_actions' }).map(function (k) { return { name: k, label: k } })
  }

  function listView(page, offset) {
    render([menu(page.key), show('Loading…')])
    call('GET', 'pages/' + encodeURIComponent(page.key) + '/rows?limit=' + PAGE_SIZE + '&offset=' + offset).then(function (data) {
      var rows = data.items || []
      var cols = columnsOf(page, rows)
      var create = formFor(page.entity, 'create')
      var status = show('')
      var head = el('tr', {}, cols.map(function (c) { return el('th', { scope: 'col', text: c.label }) }).concat([el('th', { scope: 'col', text: 'Actions' })]))
      var body = rows.map(function (r) {
        var acts = r._actions || {}
        var buttons = []
        var edit = formFor(page.entity, 'edit')
        if (edit && acts.edit) buttons.push(el('button', { type: 'button', text: 'Edit', onclick: function () { location.hash = '#/p/' + edit.key + '/' + r.id } }))
        var detail = detailFor(page.entity)
        if (detail && acts.view) buttons.push(el('button', { type: 'button', text: 'View', onclick: function () { location.hash = '#/p/' + detail.key + '/' + r.id } }))
        Object.keys(acts).filter(function (k) { return k !== 'edit' && k !== 'view' && k !== 'create' }).forEach(function (k) {
          buttons.push(el('button', { type: 'button', class: k === 'delete' ? 'danger' : '', text: k === 'delete' ? 'Delete' : label({ key: k }), onclick: function () {
            if (k === 'delete' && !window.confirm('Delete this record?')) return
            call('POST', 'pages/' + encodeURIComponent(page.key) + '/actions/' + encodeURIComponent(k), { row_id: String(r.id) }).then(function () { listView(page, offset) })
              .catch(function (e) { status.textContent = problem(e); status.className = 'error'; status.setAttribute('role', 'alert') })
          } }))
        })
        return el('tr', {}, cols.map(function (c) { return el('td', { text: cell(r[c.name]) }) }).concat([el('td', {}, [el('div', { class: 'row' }, buttons)])]))
      })
      var total = typeof data.total === 'number' ? data.total : null
      var more = total === null ? rows.length === PAGE_SIZE : offset + rows.length < total
      render([
        menu(page.key),
        el('div', { class: 'row' }, [el('h2', { text: label(page) }), create ? el('button', { class: 'primary', type: 'button', text: 'New', onclick: function () { location.hash = '#/p/' + create.key } }) : null]),
        status,
        rows.length ? el('div', { class: 'table-wrap' }, [el('table', {}, [el('thead', {}, [head]), el('tbody', {}, body)])]) : show('Nothing here yet.'),
        el('div', { class: 'row' }, [
          el('button', { type: 'button', text: 'Previous', disabled: offset === 0, onclick: function () { listView(page, Math.max(0, offset - PAGE_SIZE)) } }),
          el('button', { type: 'button', text: 'Next', disabled: !more, onclick: function () { listView(page, offset + PAGE_SIZE) } }),
        ]),
      ])
    }).catch(function (e) { sessionProblem(e, [menu(page.key), show(problem(e), 'error')]) })
  }

  function sessionProblem(e, nodes) {
    if (e instanceof ApiProblem && e.status === 401) { state.user = null; csrf = null; $('signout').hidden = true; return loginView('Your session ended. Sign in again.') }
    render(nodes)
  }

  // --- form --------------------------------------------------------------------------------------------------------------------------------------

  function input(f, value) {
    var common = { id: 'f-' + f.name, name: f.name, required: f.required && f.type !== 'boolean', disabled: f.read_only }
    if (f.type === 'enum') {
      var sel = el('select', common, [el('option', { value: '', text: '' })].concat((f.values || []).map(function (v) { return el('option', { value: v, text: v }) })))
      sel.value = value === null || value === undefined ? '' : String(value)
      return sel
    }
    if (f.type === 'boolean') { var cb = el('input', Object.assign({ type: 'checkbox' }, common)); cb.checked = value === true; cb.style.width = 'auto'; cb.style.minHeight = 'auto'; return cb }
    if (f.type === 'text' || f.type === 'json') { var ta = el('textarea', Object.assign({ rows: 4 }, common)); ta.value = value === null || value === undefined ? '' : cell(value); return ta }
    var kind = f.type === 'integer' || f.type === 'decimal' ? 'number' : f.type === 'date' ? 'date' : f.type === 'datetime' ? 'datetime-local' : 'text'
    var i = el('input', Object.assign({ type: kind, maxlength: f.max_length, step: f.type === 'decimal' ? 'any' : null }, common))
    i.value = value === null || value === undefined ? '' : String(value)
    return i
  }

  function valueOf(f, node) {
    if (f.type === 'boolean') return node.checked
    if (node.value === '') return null
    if (f.type === 'integer') return parseInt(node.value, 10)
    if (f.type === 'decimal') return node.value // decimals travel as strings: no rounding on the way
    if (f.type === 'json') { try { return JSON.parse(node.value) } catch (e) { return node.value } }
    return node.value
  }

  function formView(page, rowId) {
    var fields = (page.fields || []).filter(function (f) { return !f.read_only })
    var status = show('')
    var load = rowId ? call('GET', 'pages/' + encodeURIComponent(page.key) + '/rows/' + encodeURIComponent(rowId)) : Promise.resolve({})
    render([menu(null), show('Loading…')])
    load.then(function (row) {
      var nodes = fields.map(function (f) { return input(f, row[f.name]) })
      var back = state.pages.filter(function (p) { return p.type === 'list' && p.entity === page.entity })[0]
      render([menu(back ? back.key : null), el('form', { class: 'card', onsubmit: function (ev) {
        ev.preventDefault()
        var body = {}
        fields.forEach(function (f, i) {
          var v = valueOf(f, nodes[i])
          if (rowId || v !== null) body[f.name] = v
        })
        var path = 'pages/' + encodeURIComponent(page.key) + '/rows' + (rowId ? '/' + encodeURIComponent(rowId) : '')
        call(rowId ? 'PATCH' : 'POST', path, body).then(function () { location.hash = back ? '#/p/' + back.key : '#/' })
          .catch(function (e) { status.textContent = problem(e); status.className = 'error'; status.setAttribute('role', 'alert') })
      } }, [el('h2', { text: label(page) })].concat(fields.map(function (f, i) {
        return el('label', {}, [el('span', { text: f.label + (f.required ? ' *' : '') }), nodes[i]])
      })).concat([el('div', { class: 'row' }, [el('button', { class: 'primary', type: 'submit', text: 'Save' }),
        el('button', { type: 'button', text: 'Cancel', onclick: function () { location.hash = back ? '#/p/' + back.key : '#/' } })]), status]))])
    }).catch(function (e) { sessionProblem(e, [menu(null), show(problem(e), 'error')]) })
  }

  // --- detail ------------------------------------------------------------------------------------------------------------------------------------

  function detailView(page, rowId) {
    if (!rowId) return render([menu(null), show('Choose a record from a list.')])
    render([menu(null), show('Loading…')])
    call('GET', 'pages/' + encodeURIComponent(page.key) + '/rows/' + encodeURIComponent(rowId)).then(function (row) {
      var cols = columnsOf(page, [row])
      var back = state.pages.filter(function (p) { return p.type === 'list' && p.entity === page.entity })[0]
      var dl = el('dl', {}, [])
      cols.forEach(function (c) { dl.appendChild(el('dt', { text: c.label })); dl.appendChild(el('dd', { text: cell(row[c.name]) })) })
      render([menu(back ? back.key : null), el('div', { class: 'card' }, [el('h2', { text: label(page) }), dl,
        el('button', { type: 'button', text: 'Back', onclick: function () { location.hash = back ? '#/p/' + back.key : '#/' } })])])
    }).catch(function (e) { sessionProblem(e, [menu(null), show(problem(e), 'error')]) })
  }

  // --- start -------------------------------------------------------------------------------------------------------------------------------------

  $('signout').addEventListener('click', function () {
    call('POST', 'auth/logout').catch(function () { /* the session may already be gone */ }).then(function () {
      csrf = null; state.user = null; state.pages = []
      $('who').textContent = ''; $('signout').hidden = true
      if (navigator.serviceWorker && navigator.serviceWorker.controller) navigator.serviceWorker.controller.postMessage({ type: 'CLEAR' })
      loginView()
    })
  })
  window.addEventListener('hashchange', function () { if (state.user) route() })
  function connection() { $('offline').hidden = navigator.onLine !== false }
  window.addEventListener('online', connection); window.addEventListener('offline', connection); connection()

  // An existing session (the cookie outlives the page) gets a fresh CSRF token; no session shows the sign-in form. An anonymous application opens directly.
  call('GET', 'auth/me').then(function (u) {
    if (u.anonymous) return start(u)
    return call('GET', 'auth/csrf').then(function (r) { csrf = r.csrf_token; return start(u) })
  }).catch(function (e) {
    if (e instanceof ApiProblem && e.status === 401) return loginView()
    render([show(problem(e), 'error')])
  })

  if ('serviceWorker' in navigator && location.protocol !== 'file:') {
    navigator.serviceWorker.register('sw.js', { scope: './', updateViaCache: 'none' }).catch(function () { /* installable only where service workers work */ })
  }
})()
