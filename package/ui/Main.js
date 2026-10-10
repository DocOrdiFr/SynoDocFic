// DSM's ExtJS desktop uses Ext.extend, not the Ext 4 Ext.define API.
Ext.ns('SYNO.SDS.SynoDocFic13');

SYNO.SDS.SynoDocFic13.Instance = Ext.extend(SYNO.SDS.AppInstance, {
  appWindowName: 'SYNO.SDS.SynoDocFic13.Main',
  constructor: function () {
    SYNO.SDS.SynoDocFic13.Instance.superclass.constructor.apply(this, arguments);
  }
});

SYNO.SDS.SynoDocFic13.Main = Ext.extend(SYNO.SDS.AppWindow, {
  constructor: function (cfg) {
    var appUrl = '/webman/3rdparty/synobatchrenamer/index.html?v=1.3.2-0004';
    SYNO.SDS.SynoDocFic13.Main.superclass.constructor.call(this, Ext.apply({
      title: 'SynoDocFic',
      resizable: true,
      maximizable: true,
      minimizable: true,
      width: 1100,
      height: 760,
      minWidth: 800,
      minHeight: 560,
      layout: 'fit',
      items: [new Ext.BoxComponent({
        autoEl: {
          tag: 'iframe',
          title: 'SynoDocFic',
          src: appUrl,
          frameborder: '0',
          style: 'border:0;display:block;width:100%;height:100%'
        }
      })]
    }, cfg || {}));
  }
});
