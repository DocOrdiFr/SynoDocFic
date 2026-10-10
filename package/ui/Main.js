Ext.define('SYNO.SDS.SynoDocFic13.Instance', {
  extend: 'SYNO.SDS.AppInstance',
  appWindowName: 'SYNO.SDS.SynoDocFic13.Main',

  constructor: function () {
    this.callParent(arguments);
  }
});

Ext.define('SYNO.SDS.SynoDocFic13.Main', {
  extend: 'SYNO.SDS.AppWindow',

  constructor: function (cfg) {
    var appUrl = '/webman/3rdparty/synobatchrenamer/index.html';
    var config = Ext.apply({
      title: 'SynoDocFic',
      resizable: true,
      maximizable: true,
      minimizable: true,
      width: 1100,
      height: 760,
      minWidth: 800,
      minHeight: 560,
      layout: 'fit',
      items: [
        new Ext.BoxComponent({
          html: '<iframe title="SynoDocFic" src="' + appUrl + '" frameborder="0" width="100%" height="100%" style="display:block"></iframe>'
        })
      ]
    }, cfg);

    this.callParent([config]);
  }
});
