// Entirely synthetic DOM/transport tests. No real devices or production signals.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require(process.env.HUB_JSDOM_MODULE || 'jsdom');
const root=process.env.HUB_AC_WEB_ROOT || path.resolve(__dirname,'../src/xiaomi_ai_bridge/web');
const at=new Date().toISOString();
const settings={power:false,mode:'cool',temperature:24,fan:2,swing_vertical:1,swing_horizontal:true,turbo:false,quiet:false,light:true,ion_filter:false,xfan:false};
const mode_options=[['auto','自动'],['cool','制冷'],['dry','除湿'],['fan','送风'],['heat','制热']].map(([value,label])=>({value,label}));
const swing_options=[[0,'停止'],[1,'全范围自动'],[2,'最高位置'],[3,'偏上位置'],[4,'中间位置'],[5,'偏下位置'],[6,'最低位置'],[7,'下部自动'],[9,'中部自动'],[11,'上部自动']].map(([value,label])=>({value,label}));
const profile={key:'synthetic-ac',name:'书房空调',protocol:'kelvinator',protocol_label:'Kelvinator',settings,settings_source:'learned_signal',actual_state_known:false,ai_control_enabled:false,last_attempt:null,mode_options,swing_options};
const infrared={kind:'infrared',available:true,message:'已读取学习按键',remotes:[],learned_buttons:[{key:'synthetic-ac',label:'书房空调关机'}],learning:{supported:true,active:false,ready:false},ac_profiles:[profile],
 controls:[{action:'ir_ac_send',label:'发送空调设置',available:true,requires_confirmation:false},{action:'ir_ac_ai_permission',label:'保存空调 AI 控制权限',available:true,requires_confirmation:false}]};
const devices=[{key:'infrared',name:'万能遥控器',alias:'万能遥控器',category:'ir-remote-control',category_label:'红外遥控器',model:'chuangmi.ir.v2',enabled:true,cloud_online:true,state:{},controls:[],properties:[]}];
module.exports={infrared,devices};
async function run(){
 await require('./verify_infrared_ac_live_dom.cjs').run(root);
}
if(require.main===module)run().catch(e=>{console.error(e);process.exitCode=1;});
